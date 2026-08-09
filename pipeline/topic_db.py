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
from datetime import datetime, timedelta
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
    -- 本週（見 week_start_date()）以前還沒標完籤的舊文章會被主動捨棄，標記
    -- 這個時間戳記，避免殘留文章長期霸占每日標籤額度，也避免卡住
    -- get_unscored_topics() 的「話題內文章全標完」判斷。獨立欄位，不動
    -- content_mode 的既有語意（見 get_untagged_articles() 的說明）。
    discarded_at TEXT,
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
    -- period_start/period_end/cadence：2026-08-09 改成支援日報後補的欄位，
    -- 見下方 get_connection() 的 ALTER TABLE；舊的 7 期沒有回填，維持 NULL。
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
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN discarded_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 舊資料庫檔案已經有這個欄位
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN graph_extracted_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 舊資料庫檔案已經有這個欄位
    for column_def in ("tier TEXT", "engagement_raw REAL", "engagement_source TEXT"):
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {column_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 舊資料庫檔案已經有這個欄位
    for column_def in ("period_start TEXT", "period_end TEXT", "cadence TEXT"):
        try:
            conn.execute(f"ALTER TABLE issues ADD COLUMN {column_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 舊資料庫檔案已經有這個欄位
    return conn


def week_start_date(reference: datetime | None = None) -> str:
    """回傳「本週一」的日期（ISO date，例如 2026-07-27）。跟 fetched_at
    這種 ISO datetime 字串直接做字串比較就能得到正確的大小關係。用日曆週
    （週一重置）而不是 rolling 7 天，理由是週報本來就是一週一期，日曆週的
    邊界對應的正好是「這期收的是這週的內容」，人工審核時也比較好對照；
    rolling 窗口在測試階段執行時間不規律時，起點還會跟著飄動。"""
    ref = reference or datetime.now()
    monday = ref - timedelta(days=ref.weekday())
    return monday.strftime("%Y-%m-%d")


def insert_article_if_new(conn: sqlite3.Connection, item) -> int | None:
    """把一篇 RawItem 插進 pool，同網址已存在就跳過。回傳新插入的 article id，
    已存在則回傳 None。此時還沒做聚類/打標籤，topic_id 等欄位都是 NULL。

    tier（核心層/訊號層/深度層/垂直/case，見 config/topics.yaml 的來源分級）
    從 item.extra["tier"] 讀，沒有就當 case（多數來源目前的預設分級）。

    engagement_raw/engagement_source 只有來源本身有真實熱度數字時才寫值
    （item.extra["engagement_metric"] 由 scripts/ingest_topics.py 依
    item.source 判斷，只給 hn/reddit/github/stackexchange 這幾個訊號層
    來源設），其他來源這兩欄位維持 NULL，不是 0——0 跟「這個來源沒有
    熱度這個概念」是不同意思，不能混用。
    """
    engagement_source = item.extra.get("engagement_metric")
    engagement_raw = item.score if engagement_source else None
    try:
        cur = conn.execute(
            """INSERT INTO articles
               (source_id, source_name, source_weight, title, url, content,
                published_at, fetched_at, tier, engagement_raw, engagement_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.subdomain_id,
                item.extra.get("source_name", item.subdomain_id),
                item.extra.get("source_weight", item.score),
                item.title,
                item.url,
                item.summary,
                item.published_at.isoformat(),
                datetime.now().isoformat(),
                item.extra.get("tier", "case"),
                engagement_raw,
                engagement_source,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def backfill_article_tier(conn: sqlite3.Connection, source_name_to_tier: dict[str, str]) -> int:
    """圖上線之前入池的舊文章沒有 tier（那時這欄位還不存在），用
    config/topics.yaml 目前的 source name → tier 對照表回填，圖重建時
    歷史文章才不會缺 tier。只補 tier IS NULL 的列，已經有值的不覆蓋
    （例如之後改了某個來源的 tier 分級，不會被這支函式意外改回舊值）。
    回傳這次更新的文章數。"""
    updated = 0
    for source_name, tier in source_name_to_tier.items():
        cur = conn.execute(
            "UPDATE articles SET tier = ? WHERE source_name = ? AND tier IS NULL",
            (tier, source_name),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


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


def get_untagged_articles(conn: sqlite3.Connection, week_start: str) -> list[sqlite3.Row]:
    """回傳已經歸類到某個話題、本週（week_start 起，見 week_start_date()）
    新抓進來、但還沒跑過階段二標籤抽取的文章（content_mode IS NULL 當作
    「還沒標籤」的判斷依據）。

    只處理本週窗口內的文章，是為了避免舊的積壓文章（例如曾經因為連線層級
    錯誤卡住、後來才修好重試邏輯的那批）排在佇列前面，把當天的標籤額度佔
    光，導致當天新抓進來的文章反而標不完。窗口外的文章應該先呼叫
    discard_stale_untagged_articles() 主動標記捨棄，這裡的 fetched_at
    篩選只是雙保險，即使忘記呼叫捨棄流程也不會捞到舊文章。
    """
    return conn.execute(
        """SELECT * FROM articles
           WHERE topic_id IS NOT NULL AND content_mode IS NULL
             AND discarded_at IS NULL AND fetched_at >= ?""",
        (week_start,),
    ).fetchall()


def discard_stale_untagged_articles(conn: sqlite3.Connection, week_start: str) -> int:
    """把本週（week_start）以前、還沒標籤完的文章標記為捨棄（discarded_at）。

    這些文章通常是之前執行失敗留下的殘留（例如連線層級錯誤、額度用罄時沒
    處理到），本週已經不會再處理它們，主動標記起來才能：
    1. 讓 get_untagged_articles() 不用每次都重新掃過這些注定不會處理的舊資料
    2. 避免它們卡住 get_unscored_topics() 的「話題內文章全標完」判斷，讓
       所屬話題永遠無法打分
    只影響 discarded_at 這個獨立欄位，不動 content_mode 的既有語意，
    不代表這些文章「標籤結果是 skipped」，只是「這批不會再被標了」。
    回傳這次標記的文章數量。
    """
    cur = conn.execute(
        """UPDATE articles SET discarded_at = ?
           WHERE content_mode IS NULL AND discarded_at IS NULL AND fetched_at < ?""",
        (datetime.now().isoformat(), week_start),
    )
    conn.commit()
    return cur.rowcount


def get_ungraphed_articles(conn: sqlite3.Connection, week_start: str) -> list[sqlite3.Row]:
    """回傳已標籤、還沒跑過知識圖譜三元組抽取的文章（graph_extracted_at IS
    NULL）。只處理本週窗口內的文章，理由跟 get_untagged_articles() 一樣：
    避免舊積壓文章佔掉當天的抽取額度（這一步也要打 LLM）。"""
    return conn.execute(
        """SELECT * FROM articles
           WHERE content_mode IS NOT NULL AND graph_extracted_at IS NULL
             AND discarded_at IS NULL AND fetched_at >= ?""",
        (week_start,),
    ).fetchall()


def discard_stale_ungraphed_articles(conn: sqlite3.Connection, week_start: str) -> int:
    """跟 discard_stale_untagged_articles() 對稱：本週以前還沒建圖的文章
    標記捨棄，避免永遠卡在 get_ungraphed_articles() 的佇列裡佔額度。"""
    cur = conn.execute(
        """UPDATE articles SET discarded_at = ?
           WHERE content_mode IS NOT NULL AND graph_extracted_at IS NULL
             AND discarded_at IS NULL AND fetched_at < ?""",
        (datetime.now().isoformat(), week_start),
    )
    conn.commit()
    return cur.rowcount


def mark_article_graphed(conn: sqlite3.Connection, article_id: int) -> None:
    conn.execute(
        "UPDATE articles SET graph_extracted_at = ? WHERE id = ?",
        (datetime.now().isoformat(), article_id),
    )
    conn.commit()


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


def get_unscored_topics(
    conn: sqlite3.Connection, date_range: tuple[str, str] | None = None
) -> list[sqlite3.Row]:
    """回傳還沒做過 18 模組打分的話題（module_scores_json IS NULL）。
    只挑「話題內所有文章都已標籤完或已捨棄」的話題，避免用不完整的資訊打
    分；「已捨棄」的文章也算數，不然被捨棄的殘留文章會讓所屬話題永遠卡在
    無法打分的狀態（見 discard_stale_untagged_articles()）。

    date_range 是 (start, end) 的 ISO date 字串 tuple，有給就只回傳「底下
    至少有一篇文章 published_at 落在這個範圍（含頭尾）」的話題，供日報回填
    按天分批打分用（見 scripts/backfill_daily_issues.py）。故意不用
    topics.first_seen_at（那是系統實際跑聚類/抓取的時間，抓取本身時常有
    空窗，first_seen_at 對不上文章真正的發表日），要看的是新聞真正發生的
    那天。不給 date_range 就是原本的全池行為。
    """
    query = """SELECT t.* FROM topics t
           WHERE t.module_scores_json IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM articles a
               WHERE a.topic_id = t.id AND a.content_mode IS NULL AND a.discarded_at IS NULL
             )
             AND EXISTS (SELECT 1 FROM articles a WHERE a.topic_id = t.id)"""
    params: tuple = ()
    if date_range is not None:
        query += """ AND EXISTS (
               SELECT 1 FROM articles a
               WHERE a.topic_id = t.id AND date(a.published_at) BETWEEN ? AND ?
             )"""
        params = date_range
    return conn.execute(query, params).fetchall()


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


def get_available_topics(
    conn: sqlite3.Connection, date_range: tuple[str, str] | None = None
) -> list[sqlite3.Row]:
    """回傳已經打過分、還沒被任何一期選用過的話題。

    date_range 是 (start, end) 的 ISO date 字串 tuple，有給就只回傳「底下
    至少有一篇文章 published_at 落在這個範圍（含頭尾）」的話題，供日報選題
    把候選池限定在「這一天真正發生的新聞」（見
    scripts/compose_topic_issue.py 的 --cadence daily；不用
    topics.first_seen_at 的理由見 get_unscored_topics() 的說明）。不給就是
    原本的全池行為（weekly 用）。
    """
    query = "SELECT * FROM topics WHERE published_issue_id IS NULL AND module_scores_json IS NOT NULL"
    params: tuple = ()
    if date_range is not None:
        query += """ AND EXISTS (
               SELECT 1 FROM articles a
               WHERE a.topic_id = topics.id AND date(a.published_at) BETWEEN ? AND ?
             )"""
        params = date_range
    return conn.execute(query, params).fetchall()


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


def create_issue(
    conn: sqlite3.Connection,
    issue_date: str,
    period_start: str | None = None,
    period_end: str | None = None,
    cadence: str = "weekly",
) -> int:
    cur = conn.execute(
        """INSERT INTO issues (issue_date, created_at, period_start, period_end, cadence)
           VALUES (?, ?, ?, ?, ?)""",
        (issue_date, datetime.now().isoformat(), period_start, period_end, cadence),
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
    return conn.execute("SELECT * FROM issues ORDER BY issue_date DESC, created_at DESC").fetchall()


def _row_to_topic_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    generated = json.loads(row["generated_json"])
    source_ids = json.loads(row["source_article_ids_json"])
    return {
        **generated,
        # generated_topics.id：導讀頁卡片連到單篇話題頁（/issues/{id}/topics/{此id}）要用的識別碼，
        # 不是 topics.id（一個話題理論上可能被改寫、重新入選，generated_topics 這筆才是「這期實際發布的這篇」）。
        "id": row["id"],
        "content_type": row["content_type"],
        "confidence": row["confidence"],
        "needs_review": bool(row["needs_review"]),
        "module_scores": json.loads(row["module_scores_json"]) if row["module_scores_json"] else {},
        "sources": get_articles_by_ids(conn, source_ids),
    }


def get_issue_detail(conn: sqlite3.Connection, issue_id: int) -> dict | None:
    issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if issue is None:
        return None
    rows = conn.execute(
        """SELECT gt.*, t.content_type, t.module_scores_json FROM generated_topics gt
           JOIN topics t ON t.id = gt.topic_id
           WHERE gt.issue_id = ?""",
        (issue_id,),
    ).fetchall()
    topics = [_row_to_topic_dict(conn, row) for row in rows]
    return {"issue": issue, "topics": topics}


def get_generated_topic(conn: sqlite3.Connection, issue_id: int, generated_topic_id: int) -> dict | None:
    """單篇話題頁（/issues/{issue_id}/topics/{generated_topic_id}）用：
    多帶 issue_id 條件是為了不讓人猜別期的 generated_topic_id 就能拼出連結看到內容。"""
    row = conn.execute(
        """SELECT gt.*, t.content_type, t.module_scores_json FROM generated_topics gt
           JOIN topics t ON t.id = gt.topic_id
           WHERE gt.issue_id = ? AND gt.id = ?""",
        (issue_id, generated_topic_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_topic_dict(conn, row)
