"""話題式週報：本機網頁（PoC 階段，不處理部署/登入/權限）。

讀 SQLite 話題池產生的 issues / generated_topics，列出期數、看單期詳細內容。
跟 scripts/serve_pulse.py 是平行模組，同樣的設計。

用法：
    python -m scripts.serve_topics
    # 開瀏覽器連 http://localhost:8001（跟 serve_pulse 的 8000 錯開，方便兩邊同時開著比較）
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 這支以前純粹讀資料庫、不打 LLM，所以沒有載 .env。加了英文翻譯之後需要
# LLM_API_KEY，沒載的話 openai client 會抱怨 Missing credentials，而翻譯失敗
# 是靜默降級回原文的，不看 log 不會發現。
from dotenv import load_dotenv

load_dotenv()

from pipeline.topic_db import get_connection, get_generated_topic, get_issue_detail, list_issues
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

app = FastAPI(title=_config["newsletter"]["name"])
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


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


@app.get("/", response_class=HTMLResponse)
def issue_list(request: Request, lang: str | None = None):
    conn = _get_conn()
    issues = list_issues(conn)
    return templates.TemplateResponse(
        request,
        "topic_issue_list.html.jinja",
        {
            "newsletter_name": _config["newsletter"]["name"],
            "issues": issues,
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
    return templates.TemplateResponse(
        request,
        "topic_issue.html.jinja",
        {
            "issue_title": f"{_config['newsletter']['name']} 第{issue_id}期",
            "newsletter_name": _config["newsletter"]["name"],
            "issue_id": issue["id"],
            "issue_date": issue["issue_date"],
            "topics": topics,
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

    return templates.TemplateResponse(
        request,
        "topic_detail.html.jinja",
        {
            "issue_title": f"{_config['newsletter']['name']} 第{issue_id}期",
            "newsletter_name": _config["newsletter"]["name"],
            "issue_id": issue_id,
            "topic": topic,
            "content_type_names": _CONTENT_TYPE_NAMES,
            "lang": lang,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
