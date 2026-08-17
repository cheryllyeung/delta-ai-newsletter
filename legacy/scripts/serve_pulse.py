"""Delta Pulse v2：本機網頁（POC 階段，不處理部署/登入/權限）。

讀 SQLite 文章池產生的 issues / generated_cases，列出期數、看單期詳細內容。
部署到公司 VM 是 POC 之後的事，這裡只求本機打得開。

用法：
    python -m scripts.serve_pulse
    # 開瀏覽器連 http://localhost:8000
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.pool_db import get_connection, get_issue_detail, list_issues

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pulse.yaml"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

with open(CONFIG_PATH, encoding="utf-8") as f:
    _config = yaml.safe_load(f)

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
        "pulse_list.html.jinja",
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
        "pulse.html.jinja",
        {
            "issue_title": f"{_config['newsletter']['name']} 第{issue_id}期",
            "newsletter_name": _config["newsletter"]["name"],
            "issue_id": issue["id"],
            "issue_date": issue["issue_date"],
            "hook": issue["hook"],
            "signal": json.loads(issue["signal_json"]) if issue["signal_json"] else [],
            "cases": detail["cases"],
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
