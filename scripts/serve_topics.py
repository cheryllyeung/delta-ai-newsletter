"""話題式週報：本機網頁（PoC 階段，不處理部署/登入/權限）。

讀 SQLite 話題池產生的 issues / generated_topics，列出期數、看單期詳細內容。
跟 legacy/scripts/serve_pulse.py 是平行模組，同樣的設計。

用法：
    python -m scripts.serve_topics
    # 開瀏覽器連 http://localhost:8001（跟 serve_pulse 的 8000 錯開，方便兩邊同時開著比較）
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from collections import Counter
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 這支以前純粹讀資料庫、不打 LLM，所以沒有載 .env。加了英文翻譯之後需要
# LLM_API_KEY，沒載的話 openai client 會抱怨 Missing credentials，而翻譯失敗
# 是靜默降級回原文的，不看 log 不會發現。
from dotenv import load_dotenv

load_dotenv()

from pipeline.topic_db import (
    get_connection,
    get_generated_topic,
    get_issue_detail,
    get_recent_leaderboard_snapshots,
    list_issues,
    list_release_articles,
)
from pipeline.translate import SUPPORTED as SUPPORTED_LANGS
from pipeline.translate import get_article_in

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

with open(CONFIG_PATH, encoding="utf-8") as f:
    _config = yaml.safe_load(f)

_CONTENT_TYPE_NAMES = {ct["id"]: ct["name"] for ct in _config["content_types"]}
_MODULE_NAMES = {
    m["id"]: m["name"] for group in ("functional", "domain") for m in _config["modules"][group]
}
_DOMAIN_MODULE_IDS = {m["id"] for m in _config["modules"]["domain"]}


_TAG_MIN_SCORE = _config["selection"]["weekly"]["min_module_score_to_select"]

# 領域頁列表跟首頁卡片計數共用的顯示門檻，刻意比選題門檻低（理由見
# module_timeline）。兩處一定要用同一個值，不然會出現「首頁卡片寫 0 篇、
# 點進去卻有文章」的矛盾（2026-08-28 消費性產品實際發生過）。
_MODULE_PAGE_MIN_SCORE = 4.0


def _module_tags(module_scores: dict, exclude: str | None = None) -> list[dict]:
    """一篇文章的領域標籤：所有分數過門檻的模組，分數高到低。一篇文章天生
    帶 18 個維度的關聯度，這裡把過門檻的攤出來當可點擊的標籤
    （2026-08-26 主管反饋：一篇文章可以有很多標籤，讀者要能從自己的職能
    點進去看）。"""
    tags = [
        {
            "id": mid,
            "name": _MODULE_NAMES.get(mid, mid),
            "score": entry["score"],
            "is_domain": mid in _DOMAIN_MODULE_IDS,
        }
        for mid, entry in (module_scores or {}).items()
        if entry["score"] >= _TAG_MIN_SCORE and mid != exclude
    ]
    return sorted(tags, key=lambda t: -t["score"])


def _attach_top_module(topic: dict) -> dict:
    """在話題卡片/內文頁上標出「這篇跟台達哪個業務模組最相關」，並附上
    全部過門檻的領域標籤（module_tags），讓讀者從任何一個相關職能點進
    對應的領域頁。"""
    module_scores = topic.get("module_scores") or {}
    if module_scores:
        top_module_id = max(module_scores, key=lambda mid: module_scores[mid]["score"])
        topic["top_module_name"] = _MODULE_NAMES.get(top_module_id)
        topic["top_module_is_domain"] = top_module_id in _DOMAIN_MODULE_IDS
        topic["module_tags"] = _module_tags(module_scores)
    else:
        topic["top_module_name"] = None
        topic["top_module_is_domain"] = False
        topic["module_tags"] = []
    return topic

# 全站 HTTP Basic Auth（2026-08-26 加）。帳密放 .env 的 NEWSLETTER_WEB_USER
# ／NEWSLETTER_WEB_PASSWORD；沒設帳密時對外（0.0.0.0）啟動會直接被 main
# 擋下來拒絕啟動，本機開發（127.0.0.1）沒設就不啟用驗證，維持順手。
# 比對用 secrets.compare_digest 防 timing attack（成本為零，順手做對）。
_security = HTTPBasic(auto_error=False)

# 不用驗證的路徑：/neo4j-guide 是 Neo4j Browser 自動來抓的指南頁（它不會帶
# 帳密，擋了指南就開不起來），內容只是查詢教學、沒有刊物資料。
_AUTH_EXEMPT_PATHS = {"/neo4j-guide"}


def _basic_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(_security)):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return
    expected_user = os.environ.get("NEWSLETTER_WEB_USER", "")
    expected_password = os.environ.get("NEWSLETTER_WEB_PASSWORD", "")
    ok = (
        credentials is not None
        and secrets.compare_digest(credentials.username, expected_user)
        and secrets.compare_digest(credentials.password, expected_password)
    )
    if not ok:
        raise HTTPException(
            status_code=401, detail="帳號或密碼不對", headers={"WWW-Authenticate": "Basic"}
        )


_AUTH_ENABLED = bool(os.environ.get("NEWSLETTER_WEB_PASSWORD"))
app = FastAPI(
    title=_config["newsletter"]["name"],
    dependencies=[Depends(_basic_auth)] if _AUTH_ENABLED else [],
)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# Neo4j Browser（跑在 7474）要來抓 /neo4j-guide，那是跨來源請求，沒有 CORS
# 標頭的話瀏覽器會擋掉，Browser 只會顯示「No guide by that name」，看不出真正
# 的原因。只開放 Neo4j Browser 這個來源，不是全開。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:7474", "http://127.0.0.1:7474",
        "https://localhost:7474", "https://127.0.0.1:7474",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _get_conn():
    return get_connection(_config["database"]["path"])


def _normalise_lang(lang: str | None) -> str:
    """語言切換走伺服器端而不是前端 JS：簡體要用 opencc 做詞彙級轉換、英文要
    打 LLM 翻譯，兩件事都不適合在瀏覽器裡做。所以選單改成帶 ?lang= 重新載入。"""
    return lang if lang in SUPPORTED_LANGS else "zh-Hant"


def _localise(conn, topic: dict, lang: str) -> dict:
    """把話題字典裡的文章內容換成指定語言，非內文的欄位（id、分數、來源列）
    維持原樣，那些是版面邏輯要用的。"""
    if lang == "zh-Hant":
        return topic
    meta_keys = {"id", "content_type", "confidence", "needs_review", "module_scores", "sources",
                 "top_module_name", "top_module_is_domain"}
    body = {k: v for k, v in topic.items() if k not in meta_keys}
    translated = get_article_in(conn, topic["id"], body, lang)
    return {**topic, **translated}


@app.get("/modules/{module_id}", response_class=HTMLResponse)
def module_timeline(request: Request, module_id: str, lang: str | None = None):
    """依領域瀏覽：這個模組分數 >= 週報選題門檻的所有出刊文章，照日期新到舊。

    月曆是「按時間看」的入口，只關心特定領域的讀者需要「按領域看」的入口
    （2026-08-26 主管反饋），首頁的領域 chip 連到這裡。同一話題上過日報又上
    週報時只列一次（保留最早那期，通常是日報原文）。
    """
    modules_by_id = {
        m["id"]: {**m, "group": group}
        for group in ("functional", "domain")
        for m in _config["modules"][group]
    }
    module = modules_by_id.get(module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="沒有這個領域")

    # 顯示門檻刻意比選題門檻（6）低：消費性產品這種定義窄的模組，全池最高
    # 只有 4 分，用選題門檻整頁會空著（2026-08-28 使用者反饋「沒有話題太怪」）。
    # 4.0 跟台達專欄「次要動態」的下限（pipeline/delta_column.py 的
    # _MINOR_MIN_SCORE）是同一個標準：不夠格主打，但值得列出來。
    threshold = _MODULE_PAGE_MIN_SCORE
    conn = _get_conn()
    rows = conn.execute(
        """SELECT g.id AS generated_id, g.issue_id, g.topic_id, g.generated_json,
                  i.issue_date, i.cadence, t.module_scores_json
           FROM generated_topics g
           JOIN issues i ON i.id = g.issue_id
           JOIN topics t ON t.id = g.topic_id
           ORDER BY i.issue_date DESC, g.id"""
    ).fetchall()
    entries = []
    seen_topics: set[int] = set()
    for row in rows:
        if row["topic_id"] in seen_topics:
            continue
        scores = json.loads(row["module_scores_json"]) if row["module_scores_json"] else {}
        entry = scores.get(module_id)
        if not entry or entry["score"] < threshold:
            continue
        seen_topics.add(row["topic_id"])
        generated = json.loads(row["generated_json"])
        entries.append(
            {
                "issue_id": row["issue_id"],
                "generated_id": row["generated_id"],
                "issue_date": row["issue_date"],
                "headline": generated.get("chosen_headline") or generated.get("title") or "",
                "subhead": generated.get("chosen_subhead") or "",
                "score": entry["score"],
                "reason": entry.get("reason", ""),
                # 這篇同時掛的其他領域標籤（可點跳去那個領域頁），最多四個。
                "other_tags": _module_tags(scores, exclude=module_id)[:4],
            }
        )
    return templates.TemplateResponse(
        request,
        "topic_module_list.html.jinja",
        {
            "newsletter_name": _config["newsletter"]["name"],
            "module": module,
            "modules_by_group": {
                "domain": _config["modules"]["domain"],
                "functional": _config["modules"]["functional"],
            },
            "entries": entries,
            "lang": _normalise_lang(lang),
        },
    )


_RELEASE_KIND_NAMES = {
    "model": "模型",
    "tool": "工具",
    "hardware": "硬體",
    "dataset": "資料集",
    "benchmark": "評測榜單",
}


# 發佈頁排行榜的分類（DB source key 的後綴, 顯示名），跟
# tools/fetch_leaderboard.py 的 CATEGORIES 對應。2026-08-28 從單一總榜改成
# 分類榜：使用者要的是「各個功能（文字、程式、視覺、圖像生成）誰排最前面」。
_LEADERBOARD_CATEGORIES = [
    ("text", "文字對話"),
    ("coding", "程式開發"),
    ("vision", "視覺理解"),
    ("text-to-image", "圖像生成"),
    ("search", "搜尋"),
]


def _leaderboard_context(conn) -> list[dict]:
    """排行榜頁的資料：每個分類的最新快照，欄位照 lmarena 原樣（名次、分數、
    票數），不自己衍生任何數字。原本有拿前一天快照算的升降欄，2026-08-28
    使用者定調只呈現 arena 自己寫的東西（arena 的資料裡沒有升降欄位），
    拿掉了。還沒抓過的分類直接不出現，全部沒抓過就回空 list。"""
    boards = []
    for key, title in _LEADERBOARD_CATEGORIES:
        snaps = get_recent_leaderboard_snapshots(conn, f"lmarena-{key}", limit=1)
        if not snaps:
            continue
        latest = json.loads(snaps[0]["data_json"])
        boards.append(
            {
                "title": title,
                "fetched_at": snaps[0]["fetched_at"][:16].replace("T", " "),
                "rows": latest[:10],
            }
        )
    return boards


def _release_overview(conn) -> dict:
    """首頁「模型與工具發佈」卡片的資料：則數跟最新一則。"""
    rows = list_release_articles(conn)
    latest = None
    if rows:
        r = rows[0]
        latest = {
            "title": r["title"],
            "date": r["published_at"][:10],
            "vendor": r["release_vendor"],
        }
    return {"count": len(rows), "latest": latest}


@app.get("/releases", response_class=HTMLResponse)
def release_timeline(request: Request, vendor: str | None = None, kind: str | None = None):
    """模型與工具發佈頁：池裡所有判定為官方發佈的文章，發佈時間新到舊。

    跟領域頁（/modules/*）不同，這裡列的是池裡的文章、不是出刊文章：
    發佈快訊的價值是快與全，不用等它被選題寫成文章才看得到，標題直接連
    到原文。判定邏輯在 pipeline/release_check.py。2026-08-28 加（主管
    需求：要能看 NVIDIA、OpenAI、Google、Qwen 這些廠商最新出了什麼）。
    """
    conn = _get_conn()
    rows = [dict(r) for r in list_release_articles(conn)]
    # 依類型看（2026-08-28 使用者要求）：kind 的 id 是 release_kind 的原始值
    # （進 URL），name 是中文顯示名。沒判出類型的歸「其他」。兩個篩選可疊加。
    #
    # chip 的呈現來回改了幾次，最後定案是不對稱的（同日使用者討論）：
    # 廠商排永遠固定（全站數量，不跟著類型變），類型排跟著選中的廠商變
    # （只列那家有的類型；沒選廠商就是全站類型）。心智模型是「廠商是主要
    # 入口、類型是廠商底下的細分」。反過來讓廠商排跟著類型變的版本試過，
    # 選 arXiv＋評測榜單時 IBM 會亮起來，使用者覺得莫名其妙，不要再走回去。
    vendor_counts: Counter[str] = Counter(r["release_vendor"] or "其他" for r in rows)
    vendors = [{"name": name, "count": count} for name, count in vendor_counts.most_common()]
    kind_counts: Counter[str] = Counter(
        r["release_kind"] or "other"
        for r in rows
        if not vendor or (r["release_vendor"] or "其他") == vendor
    )
    kinds = [
        {"id": kid, "name": _RELEASE_KIND_NAMES.get(kid, "其他"), "count": count}
        for kid, count in kind_counts.most_common()
    ]
    # 換廠商時網址會帶著原本的 kind，新廠商可能沒有那個類型：選中的 chip
    # 還是要出現（標 0），不然使用者取消不掉這個篩選。
    if kind and kind not in kind_counts:
        kinds.insert(0, {"id": kind, "name": _RELEASE_KIND_NAMES.get(kind, "其他"), "count": 0})
    if vendor:
        rows = [r for r in rows if (r["release_vendor"] or "其他") == vendor]
    if kind:
        rows = [r for r in rows if (r["release_kind"] or "other") == kind]
    for r in rows:
        r["kind_name"] = _RELEASE_KIND_NAMES.get(r["release_kind"], r["release_kind"] or "")
        r["published_date"] = r["published_at"][:10]
    return templates.TemplateResponse(
        request,
        "topic_release_list.html.jinja",
        {
            "newsletter_name": _config["newsletter"]["name"],
            "entries": rows,
            "vendors": vendors,
            "active_vendor": vendor,
            "kinds": kinds,
            "active_kind": kind,
        },
    )


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_page(request: Request):
    """LMArena 分類排行榜的完整頁，資料照舊來自每小時排程存的快照。"""
    conn = _get_conn()
    return templates.TemplateResponse(
        request,
        "topic_leaderboard.html.jinja",
        {
            "newsletter_name": _config["newsletter"]["name"],
            "leaderboard": _leaderboard_context(conn),
        },
    )


@app.get("/graph", response_class=HTMLResponse)
def knowledge_graph():
    """知識圖譜的互動頁（tools/export_graph_html.py 產出的靜態檔）。
    檔案是匯出當下的快照，要更新就重跑那支工具。"""
    path = Path(__file__).resolve().parent.parent / "docs" / "knowledge_graph.html"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="還沒匯出過，先跑 python -m tools.export_graph_html",
        )
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/neo4j-guide", response_class=HTMLResponse)
def neo4j_guide(request: Request):
    """給 Neo4j Browser 用的自訂指南。連上 Neo4j 之後會自動打開這一頁，
    裡面的 Cypher 區塊在 Browser 裡是點一下就執行，不用手動打字。
    設定在 neo4j.conf 的 browser.post_connect_cmd。"""
    return templates.TemplateResponse(request, "neo4j_guide.html.jinja", {})


def _build_calendar_months(conn, issues) -> list[dict]:
    """把期數組成月曆格子（新月份在前；一週從週日起算，跟週報的週定義一致）。

    每格帶當天的期數 chip（日報顯示篇數、週報標出來），沒有刊的日子留白，
    如實呈現「那天池裡沒東西」而不是把格子藏起來。
    """
    import calendar as calendar_mod
    from datetime import date as date_cls

    counts = dict(
        conn.execute("SELECT issue_id, COUNT(*) FROM generated_topics GROUP BY issue_id").fetchall()
    )
    by_day: dict[str, list[dict]] = {}
    month_keys: set[tuple[int, int]] = set()
    for issue in issues:
        d = date_cls.fromisoformat(issue["issue_date"])
        # 週報 chip 顯示主題大標題而不是「N 則」：週報格式固定，則數沒有
        # 資訊量，主題才有（2026-08-26 主管反饋）。
        headline = None
        if issue["cadence"] == "weekly" and issue.get("column_json"):
            try:
                parsed = json.loads(issue["column_json"])
                headline = parsed.get("headline") if isinstance(parsed, dict) else None
            except ValueError:
                pass
        by_day.setdefault(issue["issue_date"], []).append(
            {
                "id": issue["id"],
                "cadence": issue["cadence"],
                "count": counts.get(issue["id"], 0),
                "headline": headline,
            }
        )
        month_keys.add((d.year, d.month))

    grid = calendar_mod.Calendar(firstweekday=6)  # 週日起算
    today_iso = date_cls.today().isoformat()
    months = []
    for year, month in sorted(month_keys, reverse=True):
        weeks = []
        for week in grid.monthdatescalendar(year, month):
            weeks.append(
                [
                    {
                        "day": d.day,
                        "in_month": d.month == month,
                        "is_today": d.isoformat() == today_iso,
                        # 格子屬於哪個月用日期判斷，chip 只放在所屬月份的格子，
                        # 避免月頭月尾的跨月格子讓同一期出現兩次。
                        "issues": by_day.get(d.isoformat(), []) if d.month == month else [],
                    }
                    for d in week
                ]
            )
        months.append({"label": f"{year} 年 {month} 月", "weeks": weeks})
    return months


def _module_overview(conn) -> dict[str, list[dict]]:
    """首頁領域卡片的資料：每個模組的累積文章數跟最新一篇。

    2026-08-26 改版：主管反饋讀者是帶著職能來的（法務的人要直接點進法務），
    首頁從「日期為主」改成「領域為主」，這個函式餵那些卡片。
    """
    rows = conn.execute(
        """SELECT g.id AS gid, g.issue_id, g.topic_id, g.generated_json,
                  i.issue_date, t.module_scores_json
           FROM generated_topics g
           JOIN issues i ON i.id = g.issue_id
           JOIN topics t ON t.id = g.topic_id
           ORDER BY i.issue_date DESC, g.id DESC"""
    ).fetchall()
    stats: dict[str, dict] = {}
    seen: dict[str, set[int]] = {}
    for row in rows:
        scores = json.loads(row["module_scores_json"]) if row["module_scores_json"] else {}
        for mid, entry in scores.items():
            if entry["score"] < _MODULE_PAGE_MIN_SCORE:
                continue
            if row["topic_id"] in seen.setdefault(mid, set()):
                continue
            seen[mid].add(row["topic_id"])
            stat = stats.setdefault(mid, {"count": 0, "latest": None})
            stat["count"] += 1
            if stat["latest"] is None:
                generated = json.loads(row["generated_json"])
                stat["latest"] = {
                    "title": generated.get("chosen_headline") or generated.get("title") or "",
                    "date": row["issue_date"],
                    "issue_id": row["issue_id"],
                    "gid": row["gid"],
                }
    overview: dict[str, list[dict]] = {}
    for group in ("domain", "functional"):
        overview[group] = [
            {
                "id": m["id"],
                "name": m["name"],
                "count": stats.get(m["id"], {}).get("count", 0),
                "latest": stats.get(m["id"], {}).get("latest"),
            }
            for m in _config["modules"][group]
        ]
    return overview


def _issue_display_no(conn, issue) -> int:
    """顯示用期數：同一種刊（daily／weekly）各自照出刊日期從 1 排，跟資料庫
    的流水號 id 脫鉤（id 經過幾輪重建已經跳號，使用者要 8/1 是第 1 期，
    2026-08-26）。網址仍然用 id，這個數字只管顯示。"""
    return conn.execute(
        """SELECT COUNT(*) FROM issues WHERE cadence = ?
           AND (issue_date < ? OR (issue_date = ? AND id <= ?))""",
        (issue["cadence"], issue["issue_date"], issue["issue_date"], issue["id"]),
    ).fetchone()[0]


@app.get("/", response_class=HTMLResponse)
def issue_list(request: Request, lang: str | None = None):
    # 2026-08-28 使用者要求：首頁只留「選你的領域」（含特輯卡），月曆跟
    # 期數列表拿掉，所以不再組 issues／months。讀者從領域頁點文章時網址
    # 還是帶期數（/issues/<id>/topics/<gid>），入口變了、內容路徑沒變。
    conn = _get_conn()
    return templates.TemplateResponse(
        request,
        "topic_issue_list.html.jinja",
        {
            "newsletter_name": _config["newsletter"]["name"],
            "module_overview": _module_overview(conn),
            "release_overview": _release_overview(conn),
            "lang": _normalise_lang(lang),
        },
    )


@app.get("/issues/{issue_id}", response_class=HTMLResponse)
def issue_overview(request: Request, issue_id: int, lang: str | None = None):
    """導讀頁：只列本期每則話題的標題／副標／一句摘要，點進卡片才看全文
    （單篇內容在 issue_topic_detail），不再整期塞成一頁長文。"""
    conn = _get_conn()
    detail = get_issue_detail(conn, issue_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="這一期不存在")

    lang = _normalise_lang(lang)
    issue = detail["issue"]
    topics = [_localise(conn, _attach_top_module(t), lang) for t in detail["topics"]]
    column_topic_count = 0
    # 台達專欄（cells）＋週報主題大標題（headline），週報限定
    # （pipeline/delta_column.py）。舊期數或日報沒有這個欄位，模板拿到
    # None 就不渲染。
    delta_column = None
    weekly_headline = None
    try:
        if issue["column_json"]:
            parsed = json.loads(issue["column_json"])
            delta_column = parsed.get("cells") if isinstance(parsed, dict) else parsed
            weekly_headline = parsed.get("headline") if isinstance(parsed, dict) else None
    except (KeyError, IndexError):
        pass
    if delta_column:
        # 專欄已經介紹過的話題，下面的文章列表不再重列（2026-08-26 使用者
        # 反映重複）。專欄格子的連結本來就指向全文，資訊沒有少。
        column_topic_ids = {c.get("topic_id") for c in delta_column}
        column_topic_count = sum(1 for t in topics if t.get("topic_id") in column_topic_ids)
        topics = [t for t in topics if t.get("topic_id") not in column_topic_ids]
    issue_no = _issue_display_no(conn, issue)
    return templates.TemplateResponse(
        request,
        "topic_issue.html.jinja",
        {
            "issue_title": f"{_config['newsletter']['name']} 第{issue_no}期",
            "newsletter_name": _config["newsletter"]["name"],
            "issue_id": issue["id"],
            "issue_no": issue_no,
            "issue_date": issue["issue_date"],
            "issue_cadence": issue["cadence"],
            "topics": topics,
            "total_articles": column_topic_count + len(topics),
            "delta_column": delta_column,
            "weekly_headline": weekly_headline,
            "content_type_names": _CONTENT_TYPE_NAMES,
            "lang": lang,
        },
    )


@app.get("/issues/{issue_id}/topics/{topic_id}", response_class=HTMLResponse)
def issue_topic_detail(request: Request, issue_id: int, topic_id: int, lang: str | None = None):
    conn = _get_conn()
    topic = get_generated_topic(conn, issue_id, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="這篇話題不存在")

    lang = _normalise_lang(lang)
    topic = _localise(conn, _attach_top_module(topic), lang)

    issue_row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    issue_no = _issue_display_no(conn, issue_row) if issue_row else issue_id
    return templates.TemplateResponse(
        request,
        "topic_detail.html.jinja",
        {
            "issue_title": f"{_config['newsletter']['name']} 第{issue_no}期",
            "newsletter_name": _config["newsletter"]["name"],
            "issue_id": issue_id,
            "issue_no": issue_no,
            "topic": topic,
            "content_type_names": _CONTENT_TYPE_NAMES,
            "lang": lang,
        },
    )


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help=(
            "監聽位址。預設 127.0.0.1（只有這台看得到）。要讓同事從公司網路連進來"
            "就用 0.0.0.0，但要注意這樣是完全沒有登入保護的，任何連得到這台的人"
            "都看得到全部內容，只適合內網分享。"
        ),
    )
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    if args.host == "0.0.0.0" and not os.environ.get("NEWSLETTER_WEB_PASSWORD"):  # noqa: S104
        # 對內網開放但沒設帳密，直接拒絕啟動（PRD 排定的權限項，2026-08-26 補上）。
        raise SystemExit(
            "[serve_topics] 要對外開放（--host 0.0.0.0）必須先在 .env 設 "
            "NEWSLETTER_WEB_USER 跟 NEWSLETTER_WEB_PASSWORD，沒有登入保護的服務不准對內網裸奔。"
        )

    if args.host == "0.0.0.0":  # noqa: S104 -- 內網分享是刻意的
        import socket

        ip = socket.gethostbyname(socket.gethostname())
        print(f"[serve_topics] 同事可以連：http://{ip}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
