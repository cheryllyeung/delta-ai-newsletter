"""把一期日報渲染成 EDM 信件的 HTML（templates/email_issue.html.jinja）。

只渲染、不寄送。寄送（Outlook 自動化）是之後的另一支，先讓排版可以
被人工檢視。輸出到 runs/email_preview_<日期>.html，用瀏覽器開即可預覽
（實際寄出後在 Outlook 裡的樣子會更保守，但版型一致）。

用法：
    python -m tools.render_issue_email               # 最新一期
    python -m tools.render_issue_email --issue-id 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.topic_db import get_connection

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "http://TWTP1NB3422.delta.corp:8002"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))

    if args.issue_id:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (args.issue_id,)).fetchone()
    else:
        issue = conn.execute("SELECT * FROM issues ORDER BY issue_date DESC, id DESC LIMIT 1").fetchone()
    if issue is None:
        print("[render_issue_email] 沒有任何一期可渲染。")
        sys.exit(1)

    rows = conn.execute(
        "SELECT * FROM generated_topics WHERE issue_id = ? ORDER BY id", (issue["id"],)
    ).fetchall()
    articles = []
    for r in rows:
        g = json.loads(r["generated_json"])
        # 摘要用卡片文案；台灣標記看打分（taiwan_industry 過 4 分就標）
        scores_row = conn.execute(
            "SELECT module_scores_json FROM topics WHERE id = ?", (r["topic_id"],)
        ).fetchone()
        tw = 0.0
        if scores_row and scores_row["module_scores_json"]:
            tw = json.loads(scores_row["module_scores_json"]).get("taiwan_industry", {}).get("score", 0)
        src = conn.execute(
            "SELECT url FROM articles WHERE topic_id = ? AND discarded_at IS NULL ORDER BY published_at DESC LIMIT 1",
            (r["topic_id"],),
        ).fetchone()
        articles.append(
            {
                "headline": g.get("chosen_headline", ""),
                "subhead": g.get("chosen_subhead", ""),
                "summary": (g.get("card_summary") or {}).get("text", ""),
                "primary_tag": g.get("primary_tag", ""),
                "is_taiwan": tw >= 4,
                "needs_review": bool(r["needs_review"]),
                "url": f"{SITE_URL}/issues/{issue['id']}/topics/{r['id']}",
                "source_url": src["url"] if src else None,
            }
        )

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    html = env.get_template("email_issue.html.jinja").render(
        newsletter_name=config["newsletter"]["name"],
        issue_title=f"{config['newsletter']['name']}（{issue['issue_date']}）",
        issue_date=issue["issue_date"],
        issue_no=issue["id"],
        articles=articles,
        site_url=SITE_URL,
    )

    out = ROOT / "runs" / f"email_preview_{issue['issue_date']}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[render_issue_email] 已輸出：{out}（{len(articles)} 則）")


if __name__ == "__main__":
    main()
