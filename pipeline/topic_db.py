"""話題式週報（第三條 pipeline）的長期資料庫：SQLite schema 與 CRUD。

跟 pipeline/pool_db.py（Delta Pulse）是平行但獨立的模組，不共用資料庫檔案，
因為資料模型不同：Delta Pulse 是「一篇文章＝一則案例」，這裡是「多篇文章
聚類成一個話題，話題才是選題/寫作的單位」（依 docs/0724_v3_PRD.md 架構）。

跟 pool_db.py 一樣刻意不用 ORM、不做硬淘汰：文章入池只做一次（依 url 去重），
之後留著；話題被選用過就標記 published_issue_id，不會被下一期重選。

文章的向量本身存在 Qdrant（pipeline/vector_store.py），這裡的 articles 表
不存向量，只存 Qdrant point id 的對應關係（直接沿用 article 自己的 id 當
Qdrant point id，兩邊用同一把 key，不需要額外欄位）。
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
    topic_id INTEGER REFERENCES topics(id),
    -- 階段二：多維關鍵詞標籤與案例標記（由 pipeline/article_tagging.py 填入）
    content_mode TEXT,
    is_case_example INTEGER,
    case_industry TEXT,
    case_department TEXT,
    case_outcome TEXT,
    tech_tags_json TEXT,
    entity_tags_json TEXT,
    scenario_tags_json TEXT,
    industry_tags_json TEXT,
    one_line_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic_id);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    representative_title TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    -- 階段三：18 模組打分（{module_id: {"score": float, "reason": str}}）+ 內容型態
    module_scores_json TEXT,
    content_type TEXT,
    published_issue_id INTEGER REFERENCES issues(id)
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generated_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    generated_json TEXT NOT NULL,
    -- 階段四檢索（pipeline/retrieval.py）實際撈給 LLM 當寫作素材的文章 id，
    -- 不一定等於這個話題自己聚類到的文章（檢索範圍是整個 pool），
    -- 存下來才能在網頁上如實顯示「這篇文章實際引用了哪些來源」。
    source_article_ids_json TEXT NOT NULL,
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
    已存在則回傳 None。此時還沒做聚類/打標籤，topic_id 等欄位都是 NULL。
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


def get_unclustered_articles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回傳還沒做過話題聚類的文章（topic_id IS NULL），供
    pipeline/topic_clustering.py 消化。"""
    return conn.execute("SELECT * FROM articles WHERE topic_id IS NULL").fetchall()


def create_topic(conn: sqlite3.Connection, representative_title: str, seen_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO topics (representative_title, first_seen_at, last_seen_at) VALUES (?, ?, ?)",
        (representative_title, seen_at, seen_at),
    )
    conn.commit()
    return cur.lastrowid


def assign_article_to_topic(conn: sqlite3.Connection, article_id: int, topic_id: int, seen_at: str) -> None:
    conn.execute("UPDATE articles SET topic_id = ? WHERE id = ?", (topic_id, article_id))
    conn.execute("UPDATE topics SET last_seen_at = ? WHERE id = ?", (seen_at, topic_id))
    conn.commit()


def get_untagged_articles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回傳已經歸類到某個話題、但還沒跑過階段二標籤抽取的文章
    （content_mode IS NULL 當作「還沒標籤」的判斷依據）。"""
    return conn.execute(
        "SELECT * FROM articles WHERE topic_id IS NOT NULL AND content_mode IS NULL"
    ).fetchall()


def save_article_tags(
    conn: sqlite3.Connection,
    article_id: int,
    content_mode: str,
    is_case_example: bool,
    case_industry: str | None,
    case_department: str | None,
    case_outcome: str | None,
    tech_tags: list[str],
    entity_tags: list[str],
    scenario_tags: list[str],
    industry_tags: list[str],
    one_line_summary: str,
) -> None:
    conn.execute(
        """UPDATE articles SET
             content_mode = ?, is_case_example = ?, case_industry = ?,
             case_department = ?, case_outcome = ?,
             tech_tags_json = ?, entity_tags_json = ?, scenario_tags_json = ?,
             industry_tags_json = ?, one_line_summary = ?
           WHERE id = ?""",
        (
            content_mode,
            int(is_case_example),
            case_industry,
            case_department,
            case_outcome,
            json.dumps(tech_tags, ensure_ascii=False),
            json.dumps(entity_tags, ensure_ascii=False),
            json.dumps(scenario_tags, ensure_ascii=False),
            json.dumps(industry_tags, ensure_ascii=False),
            one_line_summary,
            article_id,
        ),
    )
    conn.commit()


def get_unscored_topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回傳還沒做過 18 模組打分的話題（module_scores_json IS NULL）。
    只挑「話題內所有文章都已標籤完」的話題，避免用不完整的資訊打分。
    """
    return conn.execute(
        """SELECT t.* FROM topics t
           WHERE t.module_scores_json IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM articles a
               WHERE a.topic_id = t.id AND a.content_mode IS NULL
             )
             AND EXISTS (SELECT 1 FROM articles a WHERE a.topic_id = t.id)"""
    ).fetchall()


def get_articles_for_topic(conn: sqlite3.Connection, topic_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM articles WHERE topic_id = ? ORDER BY published_at DESC", (topic_id,)
    ).fetchall()


def save_module_scores(
    conn: sqlite3.Connection, topic_id: int, module_scores: dict, content_type: str
) -> None:
    conn.execute(
        "UPDATE topics SET module_scores_json = ?, content_type = ? WHERE id = ?",
        (json.dumps(module_scores, ensure_ascii=False), content_type, topic_id),
    )
    conn.commit()


def get_available_topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回傳已經打過分、還沒被任何一期選用過的話題。"""
    return conn.execute(
        "SELECT * FROM topics WHERE published_issue_id IS NULL AND module_scores_json IS NOT NULL"
    ).fetchall()


def get_articles_by_ids(conn: sqlite3.Connection, article_ids: list[int]) -> list[sqlite3.Row]:
    """依給定的 id 清單取回文章列，不保證回傳順序跟輸入順序一致（sqlite
    IN 查詢的結果順序未定義），呼叫端如果需要保序（例如 reranker 排序後
    的順序）要自己依 id 重新排列。"""
    if not article_ids:
        return []
    placeholders = ",".join("?" * len(article_ids))
    return conn.execute(
        f"SELECT * FROM articles WHERE id IN ({placeholders})", article_ids
    ).fetchall()


def get_all_scored_articles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回傳全池已標籤文章，供 pipeline/retrieval.py 的 embedding+rerank 檢索用
    （檢索範圍是整個 pool，不只是入選話題自己的文章）。"""
    return conn.execute("SELECT * FROM articles WHERE content_mode IS NOT NULL").fetchall()


def create_issue(conn: sqlite3.Connection, issue_date: str) -> int:
    cur = conn.execute(
        "INSERT INTO issues (issue_date, created_at) VALUES (?, ?)",
        (issue_date, datetime.now().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def mark_topics_published(conn: sqlite3.Connection, topic_ids: list[int], issue_id: int) -> None:
    conn.executemany(
        "UPDATE topics SET published_issue_id = ? WHERE id = ?",
        [(issue_id, tid) for tid in topic_ids],
    )
    conn.commit()


def save_generated_topic(
    conn: sqlite3.Connection,
    issue_id: int,
    topic_id: int,
    generated: dict,
    source_article_ids: list[int],
    confidence: float,
    needs_review: bool,
) -> None:
    conn.execute(
        """INSERT INTO generated_topics
           (issue_id, topic_id, generated_json, source_article_ids_json,
            confidence, needs_review, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            issue_id,
            topic_id,
            json.dumps(generated, ensure_ascii=False),
            json.dumps(source_article_ids),
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
    rows = conn.execute(
        """SELECT gt.*, t.content_type FROM generated_topics gt
           JOIN topics t ON t.id = gt.topic_id
           WHERE gt.issue_id = ?""",
        (issue_id,),
    ).fetchall()

    topics = []
    for row in rows:
        generated = json.loads(row["generated_json"])
        source_ids = json.loads(row["source_article_ids_json"])
        topics.append(
            {
                **generated,
                "content_type": row["content_type"],
                "confidence": row["confidence"],
                "needs_review": bool(row["needs_review"]),
                "sources": get_articles_by_ids(conn, source_ids),
            }
        )
    return {"issue": issue, "topics": topics}
