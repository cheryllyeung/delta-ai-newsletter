"""話題式週報（第三條 pipeline）的長期資料庫：SQLite schema 與 CRUD。

跟 legacy/pipeline/pool_db.py（Delta Pulse）是平行但獨立的模組，不共用資料庫檔案，
因為資料模型不同：Delta Pulse 是「一篇文章＝一則案例」，這裡是「多篇文章
聚類成一個話題，話題才是選題/寫作的單位」（依 docs/prd/0731_PRD_v0.5.md 架構）。

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
    one_line_summary TEXT,
    -- 收錄判定（pipeline/gates.py）。included／signal_only／excluded 三種狀態，
    -- 被擋掉的文章一樣留在表裡，只是不進標籤、打分、寫作素材，理由碼跟觸發
    -- 它的數值（gate_detail_json，例如「內文 187 字、門檻 200」）都留著，
    -- 網頁上要交代「為什麼這篇沒收」時直接讀這幾欄。
    gate_status TEXT,
    gate_reason TEXT,
    gate_detail_json TEXT,
    gate_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic_id);
-- gate_status 的索引不能放這裡：_SCHEMA 在 ALTER TABLE 之前執行，舊資料庫
-- 這時候還沒有那個欄位，CREATE INDEX 會直接炸掉。放在下面的補欄位段落之後。

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    representative_title TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    -- 階段三：18 模組打分（{module_id: {"score": float, "reason": str}}）+ 內容型態
    module_scores_json TEXT,
    content_type TEXT,
    published_issue_id INTEGER REFERENCES issues(id),
    -- 週報的出刊標記跟日報分開記：週報是「整週最好的回顧」，上過日報的
    -- 話題本來就該是週報的主要候選，不能共用 published_issue_id（共用的話
    -- 話題一上日報就永久退出週報候選池，週報只剩整週的剩菜）。2026-08-25 加。
    weekly_issue_id INTEGER REFERENCES issues(id)
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

-- 選題帳：每一期每一個候選話題的去向，入選跟落選都記一筆。
--
-- 沒有這張表以前，「為什麼這篇沒上」唯一能給的答案是「版位滿了」，因為
-- 選題邏輯跑完就只剩下入選清單，落選的連同理由一起消失在函式裡。這張表
-- 把每一次判定的結果留下來，網頁上的選題帳（/issues/<id>/trace）直接讀它。
--
-- reason 是 pipeline/gates.py 的理由碼，decision='selected' 時為 NULL。
-- detail_json 放觸發判定的實際數值（哪個模組、幾分、當時配額用掉幾個），
-- 因為理由碼只說「分數不夠」，說不出「差多少」。
CREATE TABLE IF NOT EXISTS selection_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- issue_id 可以是 NULL：選題跑完但整期生成全失敗時沒有 issue，
    -- 那次的判定紀錄還是要留著，不然就查不到「那天到底發生什麼事」。
    issue_id INTEGER REFERENCES issues(id),
    issue_date TEXT NOT NULL,
    cadence TEXT NOT NULL,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    decision TEXT NOT NULL,
    reason TEXT,
    stage TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_issue ON selection_trace(issue_id);
CREATE INDEX IF NOT EXISTS idx_trace_date ON selection_trace(issue_date);

-- 模型排行榜快照（tools/fetch_leaderboard.py 抓，/releases 頁顯示）。
-- data_json 是整份名次列表 [{"rank": 1, "model": ..., "org": ..., "score": ...}]，
-- 每次抓都存新的一列，升降是顯示時拿最近兩份快照比出來的，不另外存 delta。
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    data_json TEXT NOT NULL
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
    for column_def in (
        "tier TEXT",
        "engagement_raw REAL",
        "engagement_source TEXT",
        # 收錄判定，2026-08-14 加。舊資料維持 NULL，讀的時候一律當 included
        # （見 pipeline/gates.py 的 _status_of()），不追溯處罰已經在池裡的
        # 東西。要讓舊資料補跑一次判定用 tools/backfill_article_gates.py。
        "gate_status TEXT",
        "gate_reason TEXT",
        "gate_detail_json TEXT",
        "gate_checked_at TEXT",
        # 發佈判定，2026-08-28 加（pipeline/release_check.py）。舊資料維持
        # NULL，用 tools/backfill_release_check.py 補跑。
        "is_release INTEGER",
        "release_vendor TEXT",
        "release_product TEXT",
        "release_kind TEXT",
        "release_checked_at TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {column_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 舊資料庫檔案已經有這個欄位
    # column_json：台達專欄（週報限定），見 pipeline/delta_column.py。
    for column_def in ("period_start TEXT", "period_end TEXT", "cadence TEXT", "column_json TEXT"):
        try:
            conn.execute(f"ALTER TABLE issues ADD COLUMN {column_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 舊資料庫檔案已經有這個欄位
    # 英文版內文的快取，格式是 {"en": {...翻好的整篇 JSON...}}。簡體版不存，
    # 因為那是 opencc 的純字串轉換、即時做就好（見 pipeline/translate.py）。
    try:
        conn.execute("ALTER TABLE generated_topics ADD COLUMN translations_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 舊資料庫檔案已經有這個欄位
    # 週報出刊標記，理由見 _SCHEMA 裡 topics 表的註解。
    try:
        conn.execute("ALTER TABLE topics ADD COLUMN weekly_issue_id INTEGER REFERENCES issues(id)")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 舊資料庫檔案已經有這個欄位
    # 補完欄位才建 gate_status 的索引（理由見 _SCHEMA 裡的註解）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_gate ON articles(gate_status)")
    conn.commit()
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
    來源設），其他來源這兩欄位維持 NULL，不是 0。0 跟「這個來源沒有
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


def merge_topics(conn: sqlite3.Connection, winner_id: int, loser_id: int) -> list[int]:
    """把 loser 話題整個併進 winner：文章改掛 winner、時間範圍取聯集、
    loser 刪除。回傳被搬動的文章 id 清單（呼叫端要拿去同步 Qdrant payload）。

    2026-08-14 加。在這之前話題跟話題永遠不會合併：每篇新文章只跟最像的
    「那一篇」比，併入那篇所屬的話題。結果是同一件事的兩篇報導如果先後
    各自開了話題，之後就算再像也各走各的（實測有兩個話題都在講同一款模型、
    跨話題相似度 0.738 超過門檻，仍然是兩個話題）。

    兩個守則：

    1. **已出刊的話題不合併**（不論當 winner 還是 loser）。出刊記錄
       （generated_topics、selection_trace）指著 topic_id，合併會讓歷史
       記錄指到一個內容已經變了的話題。呼叫端要先檢查 published_issue_id。
    2. **合併後 winner 的打分歸零**（module_scores_json 設回 NULL）。話題的
       文章集合變了，舊分數是對舊集合打的。歸零之後 ingest 流程的打分步驟
       （排在聚類後面）同一輪就會用完整的文章集合重打，代價是每次合併多
       一次打分呼叫，換來的是分數永遠對得上內容。
    """
    moved = [
        row["id"]
        for row in conn.execute("SELECT id FROM articles WHERE topic_id = ?", (loser_id,))
    ]
    conn.execute("UPDATE articles SET topic_id = ? WHERE topic_id = ?", (winner_id, loser_id))
    conn.execute(
        """UPDATE topics SET
             first_seen_at = MIN(first_seen_at, (SELECT first_seen_at FROM topics WHERE id = ?)),
             last_seen_at  = MAX(last_seen_at,  (SELECT last_seen_at  FROM topics WHERE id = ?)),
             module_scores_json = NULL
           WHERE id = ?""",
        (loser_id, loser_id, winner_id),
    )
    # loser 可能還被歷史紀錄引用著（例如重建期數時保留下來當重用快取的
    # generated_topics 列，它的 published_issue_id 已被清空所以通過了
    # 「已出刊不合併」的檢查）。直接刪會撞外鍵，先把引用改指到 winner，
    # 語意也正確：那篇舊文章寫的內容現在屬於合併後的話題。2026-08-26 修。
    conn.execute(
        "UPDATE generated_topics SET topic_id = ? WHERE topic_id = ?", (winner_id, loser_id)
    )
    conn.execute(
        "UPDATE selection_trace SET topic_id = ? WHERE topic_id = ?", (winner_id, loser_id)
    )
    conn.execute("DELETE FROM topics WHERE id = ?", (loser_id,))
    conn.commit()
    return moved


def get_topic_published_map(conn: sqlite3.Connection, topic_ids: list[int]) -> dict[int, bool]:
    """回傳 {topic_id: 是否已出刊}。聚類的合併判斷用：已出刊的話題不參與合併。"""
    if not topic_ids:
        return {}
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"SELECT id, published_issue_id FROM topics WHERE id IN ({placeholders})", topic_ids
    ).fetchall()
    return {row["id"]: row["published_issue_id"] is not None for row in rows}


def get_untagged_articles(conn: sqlite3.Connection, week_start: str) -> list[sqlite3.Row]:
    """回傳已經歸類到某個話題、本週（week_start 起，見 week_start_date()）
    新抓進來、但還沒跑過階段二標籤抽取的文章（content_mode IS NULL 當作
    「還沒標籤」的判斷依據）。

    只處理本週窗口內的文章，是為了避免舊的積壓文章（例如曾經因為連線層級
    錯誤卡住、後來才修好重試邏輯的那批）排在佇列前面，把當天的標籤額度佔
    光，導致當天新抓進來的文章反而標不完。窗口外的文章應該先呼叫
    discard_stale_untagged_articles() 主動標記捨棄，這裡的 fetched_at
    篩選只是雙保險，即使忘記呼叫捨棄流程也不會撈到舊文章。

    沒通過 Gate 1a 的文章（只有標題的 signal_only、超出窗口的 excluded）
    不在這裡回傳：標籤每篇要打一次 gateway，池裡有 43% 是只有標題的殘缺
    內容，先擋掉省下來的是每天實際要等的時間。gate_status IS NULL 是
    2026-08-14 以前入池的舊資料，當作 included 照舊處理。
    """
    return conn.execute(
        """SELECT * FROM articles
           WHERE topic_id IS NOT NULL AND content_mode IS NULL
             AND discarded_at IS NULL AND fetched_at >= ?
             AND COALESCE(gate_status, 'included') = 'included'""",
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
    避免舊積壓文章佔掉當天的抽取額度（這一步也要打 LLM）。

    沒通過 Gate 1 的文章不建圖：一行標題抽不出有意義的三元組，抽出來的
    只會是實體之間的假關係，反而汙染圖。"""
    return conn.execute(
        """SELECT * FROM articles
           WHERE content_mode IS NOT NULL AND graph_extracted_at IS NULL
             AND discarded_at IS NULL AND fetched_at >= ?
             AND COALESCE(gate_status, 'included') = 'included'""",
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


def save_article_gate(conn: sqlite3.Connection, article_id: int, result) -> None:
    """把一次 Gate 1 判定的結果寫回文章列（result 是 pipeline.gates.GateResult）。

    每次都覆寫，不做「已經判過就跳過」：門檻值改了之後補跑判定要能反映新的
    設定，而 gate_checked_at 會記下這次是什麼時候判的，回頭對照得出來哪些
    是舊門檻下的結果。
    """
    conn.execute(
        """UPDATE articles SET gate_status = ?, gate_reason = ?,
             gate_detail_json = ?, gate_checked_at = ? WHERE id = ?""",
        (
            result.status,
            result.reason,
            json.dumps(result.detail, ensure_ascii=False) if result.detail else None,
            datetime.now().isoformat(),
            article_id,
        ),
    )
    conn.commit()


def get_release_unchecked_articles(
    conn: sqlite3.Connection, week_start: str | None = None
) -> list[sqlite3.Row]:
    """已標籤但還沒跑過發佈判定的文章（release_checked_at IS NULL）。

    ingest 帶本週窗口（跟標籤同一套「舊積壓不佔當天額度」的邏輯）；
    tools/backfill_release_check.py 不帶窗口，補整個池。只判 included 的
    文章：signal_only 只有一行標題，判出來也沒有摘要可以顯示。
    """
    sql = """SELECT * FROM articles
             WHERE content_mode IS NOT NULL AND release_checked_at IS NULL
               AND discarded_at IS NULL
               AND COALESCE(gate_status, 'included') = 'included'"""
    params: tuple = ()
    if week_start is not None:
        sql += " AND fetched_at >= ?"
        params = (week_start,)
    return conn.execute(sql + " ORDER BY id", params).fetchall()


# 同一家公司模型會給不同稱呼（實測 20 篇裡 AWS 跟 Amazon 混用），發佈頁的
# 廠商篩選會因此裂成兩個 chip。在寫入端收斂而不是改 prompt：prompt 管不住
# 每一次輸出，這裡管得住。鍵一律小寫比對。
_VENDOR_ALIASES = {
    "amazon": "AWS",
    "amazon web services": "AWS",
    "google deepmind": "Google",
    "google cloud": "Google",
    "meta ai": "Meta",
    "microsoft azure": "Microsoft",
    "alibaba cloud": "Alibaba",
    "qwen": "Alibaba",
}


def _canonical_vendor(vendor: str | None) -> str | None:
    if not vendor:
        return None
    return _VENDOR_ALIASES.get(vendor.strip().lower(), vendor.strip())


def save_article_release(conn: sqlite3.Connection, article_id: int, parsed: dict) -> None:
    """把發佈判定的結果寫回文章列（parsed 是 release_check prompt 的輸出）。

    不是發佈時三個描述欄位一律歸 NULL，不留模型順手填的雜訊；
    release_checked_at 一律寫，這是「判過了」的依據，跟結果無關。
    """
    is_release = bool(parsed.get("is_release"))
    conn.execute(
        """UPDATE articles SET is_release = ?, release_vendor = ?,
             release_product = ?, release_kind = ?, release_checked_at = ?
           WHERE id = ?""",
        (
            int(is_release),
            _canonical_vendor(parsed.get("vendor")) if is_release else None,
            parsed.get("product") if is_release else None,
            parsed.get("release_kind") if is_release else None,
            datetime.now().isoformat(),
            article_id,
        ),
    )
    conn.commit()


def list_release_articles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """發佈頁（/releases）的資料：所有判定為發佈的文章，發佈時間新到舊。

    列的是池裡的文章不是出刊文章：發佈快訊的價值是快與全，不用等它寫成
    文章才看得到，連結直接指向原文。
    """
    return conn.execute(
        """SELECT id, source_name, title, url, published_at,
                  release_vendor, release_product, release_kind, one_line_summary
           FROM articles
           WHERE is_release = 1 AND discarded_at IS NULL
           ORDER BY published_at DESC"""
    ).fetchall()


def save_leaderboard_snapshot(conn: sqlite3.Connection, source: str, data: list[dict]) -> None:
    conn.execute(
        "INSERT INTO leaderboard_snapshots (source, fetched_at, data_json) VALUES (?, ?, ?)",
        (source, datetime.now().isoformat(), json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()


def get_recent_leaderboard_snapshots(
    conn: sqlite3.Connection, source: str, limit: int = 2
) -> list[sqlite3.Row]:
    """最近幾份榜單快照，新的在前。"""
    return conn.execute(
        """SELECT * FROM leaderboard_snapshots WHERE source = ?
           ORDER BY id DESC LIMIT ?""",
        (source, limit),
    ).fetchall()




def get_ungated_articles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回傳還沒跑過 Gate 1a 的文章（gate_status IS NULL）。

    正常流程下每篇文章入池當下就會判定，這支是給補跑用的：2026-08-14 以前
    入池的文章沒有這個欄位，門檻值調整後也需要整批重判（見
    tools/backfill_article_gates.py）。
    """
    return conn.execute("SELECT * FROM articles WHERE gate_status IS NULL").fetchall()


def get_gate_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """依來源統計 Gate 1 的判定結果，供選題帳跟報告用。

    這張表是「這個來源到底有沒有在貢獻可用內容」最直接的證據：某個來源
    100% 都是 signal_only，代表它給的一直只有標題，該考慮換抓法還是拿掉。
    """
    return conn.execute(
        """SELECT source_name,
                  COUNT(*) AS total,
                  SUM(CASE WHEN COALESCE(gate_status,'included')='included' THEN 1 ELSE 0 END) AS included,
                  SUM(CASE WHEN gate_status='signal_only' THEN 1 ELSE 0 END) AS signal_only,
                  SUM(CASE WHEN gate_status='excluded' THEN 1 ELSE 0 END) AS excluded
           FROM articles GROUP BY source_name ORDER BY total DESC"""
    ).fetchall()


def record_selection_trace(
    conn: sqlite3.Connection,
    *,
    issue_date: str,
    cadence: str,
    entries: list[dict],
    issue_id: int | None = None,
) -> None:
    """一次寫入這一期所有候選話題的去向。

    entries 的每個元素是 {"topic_id", "decision", "reason", "stage", "detail"}，
    decision 是 "selected" 或 "rejected"，stage 是判定發生在哪一步
    （"selection" 或 "generation"），detail 是可 JSON 序列化的 dict。

    選題跟生成是兩次呼叫（選完才知道生成會不會失敗），所以這支設計成可以
    對同一期呼叫多次累加，不是一次寫完就鎖住。
    """
    now = datetime.now().isoformat()
    conn.executemany(
        """INSERT INTO selection_trace
             (issue_id, issue_date, cadence, topic_id, decision, reason, stage, detail_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                issue_id,
                issue_date,
                cadence,
                e["topic_id"],
                e["decision"],
                e.get("reason"),
                e.get("stage", "selection"),
                json.dumps(e.get("detail") or {}, ensure_ascii=False),
                now,
            )
            for e in entries
        ],
    )
    conn.commit()


def attach_trace_to_issue(conn: sqlite3.Connection, issue_date: str, cadence: str, issue_id: int) -> None:
    """選題階段寫 trace 的時候還沒有 issue_id（那一期要等生成成功才建立），
    出刊成功後回頭把同一天同一個 cadence 的 trace 補上 issue_id，網頁才能
    從某一期連到它的選題帳。"""
    conn.execute(
        """UPDATE selection_trace SET issue_id = ?
           WHERE issue_id IS NULL AND issue_date = ? AND cadence = ?""",
        (issue_id, issue_date, cadence),
    )
    conn.commit()


def get_selection_trace(conn: sqlite3.Connection, issue_id: int) -> list[sqlite3.Row]:
    """回傳某一期的選題帳，入選的排前面，其餘依理由碼分組。"""
    return conn.execute(
        """SELECT st.*, t.representative_title, t.content_type
           FROM selection_trace st JOIN topics t ON t.id = st.topic_id
           WHERE st.issue_id = ?
           ORDER BY CASE st.decision WHEN 'selected' THEN 0 ELSE 1 END, st.reason, st.id""",
        (issue_id,),
    ).fetchall()


def get_unscored_topics(
    conn: sqlite3.Connection, date_range: tuple[str, str] | None = None
) -> list[sqlite3.Row]:
    """回傳還沒做過 18 模組打分的話題（module_scores_json IS NULL）。
    只挑「話題內所有文章都已標籤完或已捨棄」的話題，避免用不完整的資訊打
    分；「已捨棄」的文章也算數，不然被捨棄的殘留文章會讓所屬話題永遠卡在
    無法打分的狀態（見 discard_stale_untagged_articles()）。

    date_range 是 (start, end) 的 ISO date 字串 tuple，有給就只回傳「底下
    至少有一篇文章 published_at 落在這個範圍（含頭尾）」的話題，供日報回填
    按天分批打分用（見 tools/backfill_daily_issues.py）。故意不用
    topics.first_seen_at（那是系統實際跑聚類/抓取的時間，抓取本身時常有
    空窗，first_seen_at 對不上文章真正的發表日），要看的是新聞真正發生的
    那天。不給 date_range 就是原本的全池行為。
    """
    # 「話題內文章全標完」只看 gate_status='included' 的文章：signal_only
    # 跟 excluded 的本來就不會去標，把它們算進未標籤清單會讓所屬話題永遠
    # 卡在無法打分（跟 discarded_at 要排除掉是同一個道理）。
    # 最後一個 EXISTS 也要跟著只看 included，否則底下全是殘缺標題的話題
    # 會通過打分，打完分再被 Gate 2 擋掉，白花一次 gateway 呼叫。
    query = """SELECT t.* FROM topics t
           WHERE t.module_scores_json IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM articles a
               WHERE a.topic_id = t.id AND a.content_mode IS NULL AND a.discarded_at IS NULL
                 AND COALESCE(a.gate_status, 'included') = 'included'
             )
             AND EXISTS (
               SELECT 1 FROM articles a
               WHERE a.topic_id = t.id AND COALESCE(a.gate_status, 'included') = 'included'
             )"""
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
    conn: sqlite3.Connection,
    date_range: tuple[str, str] | None = None,
    cadence: str = "daily",
) -> list[sqlite3.Row]:
    """回傳已經打過分、還沒被「這個出刊頻率」選用過的話題。

    date_range 是 (start, end) 的 ISO date 字串 tuple，有給就只回傳「底下
    至少有一篇文章 published_at 落在這個範圍（含頭尾）」的話題，供日報選題
    把候選池限定在「這一天真正發生的新聞」（見
    scripts/compose_topic_issue.py 的 --cadence daily；不用
    topics.first_seen_at 的理由見 get_unscored_topics() 的說明）。

    cadence="weekly" 時改看 weekly_issue_id 而不是 published_issue_id：
    週報是「整週最好的回顧」，上過日報的話題本來就該進週報候選池，只排除
    已經上過週報的（欄位設計理由見 _SCHEMA 裡 topics 表的註解，2026-08-25）。
    """
    exclusion_column = "weekly_issue_id" if cadence == "weekly" else "published_issue_id"
    query = f"SELECT * FROM topics WHERE {exclusion_column} IS NULL AND module_scores_json IS NOT NULL"
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


def mark_topics_published(
    conn: sqlite3.Connection, topic_ids: list[int], issue_id: int, cadence: str = "daily"
) -> None:
    """cadence="weekly" 時寫 weekly_issue_id，日報跟週報的出刊標記互不影響
    （理由見 _SCHEMA 裡 topics 表的註解）。"""
    column = "weekly_issue_id" if cadence == "weekly" else "published_issue_id"
    conn.executemany(
        f"UPDATE topics SET {column} = ? WHERE id = ?",
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
    translations_json: str | None = None,
) -> None:
    """translations_json 給週報重用日報文章時把翻譯快取一起帶過來用（見
    get_latest_generated_for_topics()），正常生成流程不帶、留給
    pipeline/translate.py 的 pretranslate_issue() 補。"""
    conn.execute(
        """INSERT INTO generated_topics
           (issue_id, topic_id, generated_json, source_article_ids_json,
            confidence, needs_review, created_at, translations_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            issue_id,
            topic_id,
            json.dumps(generated, ensure_ascii=False),
            json.dumps(source_article_ids),
            confidence,
            int(needs_review),
            datetime.now().isoformat(),
            translations_json,
        ),
    )
    conn.commit()


def get_latest_generated_for_topics(
    conn: sqlite3.Connection, topic_ids: list[int]
) -> dict[int, sqlite3.Row]:
    """回傳 {topic_id: 該話題最近一次生成的 generated_topics 列}。

    週報選到已經上過日報的話題時，文章（含自檢信心度與翻譯快取）直接重用
    日報那份，不重寫也不重新自檢：同一個話題重寫一次是重花一次 LLM 又拿到
    一篇沒被看過的新文章，重用才符合「週報是整週最好的回顧」。2026-08-25 加。
    """
    if not topic_ids:
        return {}
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"""SELECT * FROM generated_topics
            WHERE topic_id IN ({placeholders})
            ORDER BY created_at""",
        topic_ids,
    ).fetchall()
    # 同一話題有多筆時保留 created_at 最新的（依序覆蓋）。
    return {row["topic_id"]: row for row in rows}


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
        # topics.id：週報導讀頁用來跟台達專欄比對「這篇是不是已經在專欄
        # 出現過」（scripts/serve_topics.py 的 issue_overview）。
        "topic_id": row["topic_id"],
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
