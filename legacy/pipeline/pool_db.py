"""Delta Pulse v2 的長期文章池：SQLite schema 與 CRUD。

設計原則：入池只做一次（依 url 去重），之後不管評分高低都留著，不做
true/false 的硬淘汰。「這篇文章這期要不要用」是每次出刊時對這個 pool
下一次查詢決定的，不是在抓取當下就決定生死。

POC 階段刻意不用 ORM，标准庫 sqlite3 加幾支函式就夠，之後真的要接公司
VM/多人並發存取，再評估要不要換成真正的伺服器型資料庫。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_weight REAL NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    area_category TEXT,
    is_ai_application INTEGER,
    transferability REAL,
    specificity REAL,
    novelty REAL,
    narrativity REAL,
    base_score REAL,
    one_line_summary TEXT,
    key_facts_json TEXT,
    published_issue_id INTEGER REFERENCES issues(id)
);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id INTEGER NOT NULL REFERENCES articles(id),
    tag TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag);
CREATE INDEX IF NOT EXISTS idx_article_tags_article ON article_tags(article_id);

CREATE TABLE IF NOT EXISTS tag_aliases (
    raw_tag TEXT PRIMARY KEY,
    canonical_tag TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_date TEXT NOT NULL,
    hook TEXT,
    signal_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generated_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    generated_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    needs_review INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def insert_article_if_new(conn: sqlite3.Connection, item) -> int | None:
    """把一篇 RawItem 插進 pool，同網址已存在就跳過。回傳新插入的 article id，
    已存在則回傳 None（呼叫端可以用這個判斷要不要跳過評分）。
    """
    try:
        cur = conn.execute(
            """INSERT INTO articles
               (source_id, source_name, source_weight, title, url, content,
                published_at, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.subdomain_id,
                item.extra.get("source_name", item.subdomain_id),
                item.extra.get("source_weight", item.score),
                item.title,
                item.url,
                item.summary,
                item.published_at.isoformat(),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_unscored_articles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM articles WHERE base_score IS NULL"
    ).fetchall()


def save_score(
    conn: sqlite3.Connection,
    article_id: int,
    area_category: str,
    is_ai_application: bool,
    scores: dict,
    base_score: float,
    one_line_summary: str,
    key_facts: list,
    hashtags: list[str],
) -> None:
    conn.execute(
        """UPDATE articles SET
             area_category = ?, is_ai_application = ?,
             transferability = ?, specificity = ?, novelty = ?, narrativity = ?,
             base_score = ?, one_line_summary = ?, key_facts_json = ?
           WHERE id = ?""",
        (
            area_category,
            int(is_ai_application),
            scores["transferability"],
            scores["specificity"],
            scores["novelty"],
            scores["narrativity"],
            base_score,
            one_line_summary,
            json.dumps(key_facts, ensure_ascii=False),
            article_id,
        ),
    )
    conn.executemany(
        "INSERT INTO article_tags (article_id, tag) VALUES (?, ?)",
        [(article_id, tag) for tag in hashtags],
    )
    conn.commit()


def get_tag_counts_since(conn: sqlite3.Connection, since_iso: str) -> dict[str, int]:
    """統計 tag 出現次數，會先把原始標籤透過 tag_aliases 轉成 canonical 寫法
    再合併計數（見 pipeline/tag_clustering.py），沒有對應別名的標籤就用原字。
    """
    rows = conn.execute(
        """SELECT COALESCE(ta.canonical_tag, t.tag) AS tag, COUNT(*) AS n
           FROM article_tags t
           JOIN articles a ON a.id = t.article_id
           LEFT JOIN tag_aliases ta ON ta.raw_tag = t.tag
           WHERE a.published_at >= ?
           GROUP BY COALESCE(ta.canonical_tag, t.tag)""",
        (since_iso,),
    ).fetchall()
    return {row["tag"]: row["n"] for row in rows}


def set_tag_aliases(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    """整批覆寫標籤別名對照表（pipeline.tag_clustering.compute_tag_clusters() 的輸出）。"""
    conn.execute("DELETE FROM tag_aliases")
    conn.executemany(
        "INSERT INTO tag_aliases (raw_tag, canonical_tag) VALUES (?, ?)",
        list(mapping.items()),
    )
    conn.commit()


def get_available_pool(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回傳還沒被任何一期選用過、且已經評分過的文章。"""
    return conn.execute(
        """SELECT * FROM articles
           WHERE published_issue_id IS NULL AND base_score IS NOT NULL"""
    ).fetchall()


def get_tags_for_article(conn: sqlite3.Connection, article_id: int) -> list[str]:
    """回傳這篇文章的標籤（canonical 寫法，見 get_tag_counts_since 的說明）。"""
    rows = conn.execute(
        """SELECT DISTINCT COALESCE(ta.canonical_tag, t.tag) AS tag
           FROM article_tags t
           LEFT JOIN tag_aliases ta ON ta.raw_tag = t.tag
           WHERE t.article_id = ?""",
        (article_id,),
    ).fetchall()
    return [row["tag"] for row in rows]


def create_issue(conn: sqlite3.Connection, issue_date: str, hook: str, signal: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO issues (issue_date, hook, signal_json, created_at) VALUES (?, ?, ?, ?)",
        (issue_date, hook, json.dumps(signal, ensure_ascii=False), datetime.now().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def mark_published(conn: sqlite3.Connection, article_ids: list[int], issue_id: int) -> None:
    conn.executemany(
        "UPDATE articles SET published_issue_id = ? WHERE id = ?",
        [(issue_id, aid) for aid in article_ids],
    )
    conn.commit()


def save_generated_case(
    conn: sqlite3.Connection,
    issue_id: int,
    article_id: int,
    generated: dict,
    confidence: float,
    needs_review: bool,
) -> None:
    conn.execute(
        """INSERT INTO generated_cases
           (issue_id, article_id, generated_json, confidence, needs_review, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            issue_id,
            article_id,
            json.dumps(generated, ensure_ascii=False),
            confidence,
            int(needs_review),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()


def list_issues(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM issues ORDER BY issue_date DESC").fetchall()


def get_issue_detail(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if issue is None:
        return None
    case_rows = conn.execute(
        """SELECT gc.*, a.source_name, a.area_category, a.url
           FROM generated_cases gc JOIN articles a ON a.id = gc.article_id
           WHERE gc.issue_id = ?""",
        (issue_id,),
    ).fetchall()

    cases = []
    for row in case_rows:
        generated = json.loads(row["generated_json"])
        cases.append(
            {
                **generated,
                "source_name": row["source_name"],
                "area_category": row["area_category"],
                "url": row["url"],
                "confidence": row["confidence"],
                "needs_review": bool(row["needs_review"]),
                "tags": get_tags_for_article(conn, row["article_id"]),
            }
        )
    return {"issue": issue, "cases": cases}
