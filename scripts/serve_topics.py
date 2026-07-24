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

from pipeline.topic_db import get_connection, get_issue_detail, list_issues

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

with open(CONFIG_PATH, encoding="utf-8") as f:
    _config = yaml.safe_load(f)

_CONTENT_TYPE_NAMES = {ct["id"]: ct["name"] for ct in _config["content_types"]}

app = FastAPI(title=_config["newsletter"]["name"])
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _get_conn():
    return get_connection(_config["database"]["path"])


@app.get("/", response_class=HTMLResponse)
def issue_list(request: Request):
    conn = _get_conn()
    issues = list_issues(conn)
    return templates.TemplateResponse(
        request,
        "topic_issue_list.html.jinja",
        {
            "newsletter_name": _config["newsletter"]["name"],
            "issues": issues,
        },
    )


@app.get("/issues/{issue_id}", response_class=HTMLResponse)
def issue_detail(request: Request, issue_id: int):
    conn = _get_conn()
    detail = get_issue_detail(conn, issue_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="這一期不存在")

    issue = detail["issue"]
    return templates.TemplateResponse(
        request,
        "topic_issue.html.jinja",
        {
            "issue_title": f"{_config['newsletter']['name']} 第{issue_id}期",
            "newsletter_name": _config["newsletter"]["name"],
            "issue_id": issue["id"],
            "issue_date": issue["issue_date"],
            "topics": detail["topics"],
            "content_type_names": _CONTENT_TYPE_NAMES,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
