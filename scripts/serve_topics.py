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
    list_issues,
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


def _attach_top_module(topic: dict) -> dict:
    """在話題卡片/內文頁上標出「這篇跟台達哪個業務模組最相關」，讓讀者一眼
    看出選題邏輯不是隨機湊數，尤其是 domain（本業）模組要讓讀者看得到。"""
    module_scores = topic.get("module_scores") or {}
    if module_scores:
        top_module_id = max(module_scores, key=lambda mid: module_scores[mid]["score"])
        topic["top_module_name"] = _MODULE_NAMES.get(top_module_id)
        topic["top_module_is_domain"] = top_module_id in _DOMAIN_MODULE_IDS
    else:
        topic["top_module_name"] = None
        topic["top_module_is_domain"] = False
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

    threshold = _config["selection"]["weekly"]["min_module_score_to_select"]
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
    conn = _get_conn()
    issues = [dict(row) for row in list_issues(conn)]
    for issue in issues:
        issue["no"] = _issue_display_no(conn, issue)
    return templates.TemplateResponse(
        request,
        "topic_issue_list.html.jinja",
        {
            "newsletter_name": _config["newsletter"]["name"],
            "issues": issues,
            "months": _build_calendar_months(conn, issues),
            "modules_by_group": {
                "domain": _config["modules"]["domain"],
                "functional": _config["modules"]["functional"],
            },
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
