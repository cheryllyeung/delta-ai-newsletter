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
from markupsafe import Markup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.edm_tags import is_primary, tags_for_topic
from pipeline.text_emphasis import emphasize_numbers
from pipeline.issue_tldr import tldr_display_groups
from pipeline.topic_db import get_connection

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "http://TWTP1NB3422.delta.corp:8002"


def _with_emphasis(sections):
    """把內文的數字標成粗體（pipeline/text_emphasis.py）。轉義與加粗都在
    程式端做，不讓模型輸出 HTML。"""
    if not sections:
        return sections
    out = []
    for sec in sections:
        body = sec.get("body") or sec.get("text") or ""
        out.append({**sec, "body_html": Markup(emphasize_numbers(body))})
    return out


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

    # 分類 -> 專屬色（雜誌版用，低彩度）
    tag_color = {
        "廠商動態": "#8a5a1a", "技術發表": "#2a6a5a", "研究成果": "#2a6a5a",
        "法規與給付": "#6a3a6a", "臨床應用": "#8a2a3a", "財務與併購": "#3a5a8a",
        "台灣動態": "#8a5a1a", "其他": "#5a6672",
    }
    rows = conn.execute(
        "SELECT * FROM generated_topics WHERE issue_id = ? ORDER BY id", (issue["id"],)
    ).fetchall()
    articles = []
    for idx, r in enumerate(rows, 1):
        g = json.loads(r["generated_json"])
        # 摘要用卡片文案；台灣標記看打分（taiwan_industry 過 4 分就標）
        scores_row = conn.execute(
            "SELECT module_scores_json FROM topics WHERE id = ?", (r["topic_id"],)
        ).fetchone()
        tw = 0.0
        if scores_row and scores_row["module_scores_json"]:
            tw = json.loads(scores_row["module_scores_json"]).get("taiwan_industry", {}).get("score", 0)
        # 主要來源那一篇（連結指向它）＋幾家在報。2026-09-30 起 EDM 不放
        # 全文，標題與「看原文」都連原文網站，所以這裡要拿到來源名稱。
        src = conn.execute(
            """SELECT url, source_name FROM articles
               WHERE topic_id = ? AND discarded_at IS NULL
                 AND (gate_status IS NULL OR gate_status != 'excluded')
               ORDER BY published_at DESC LIMIT 1""",
            (r["topic_id"],),
        ).fetchone()
        source_count = conn.execute(
            """SELECT count(DISTINCT source_id) c FROM articles
               WHERE topic_id = ? AND discarded_at IS NULL
                 AND (gate_status IS NULL OR gate_status != 'excluded')""",
            (r["topic_id"],),
        ).fetchone()["c"]
        tag = g.get("primary_tag", "其他")
        articles.append(
            {
                "num": f"{idx:02d}",
                "headline": g.get("chosen_headline", ""),
                "subhead": g.get("chosen_subhead", ""),
                "summary": (g.get("card_summary") or {}).get("text", ""),
                "primary_tag": tag,
                "tag_color": tag_color.get(tag, "#5a6672"),
                "is_taiwan": tw >= 4,
                "url": f"{SITE_URL}/issues/{issue['id']}/topics/{r['id']}",
                "source_url": src["url"] if src else None,
                "source_name": src["source_name"] if src else "原文",
                "source_count": source_count,
                # 標籤（廠商／產品／技術）與分區都經過原文比對，見
                # pipeline/edm_tags.py：模型抽的標籤若在原文找不到就丟掉。
                "tags": tags_for_topic(conn, r["topic_id"], config),
                "is_primary": is_primary(conn, r["topic_id"], config),
                "needs_review": bool(r["needs_review"]),
                # 2026-09-18 信件自包含：完整內容直接放進信裡，收件人不用
                # 連回筆電上的網頁伺服器也讀得到（筆電關機連結就死）。
                # 自檢信心偏低（needs_review）的不放全文，只給摘要與原文
                # 連結（使用者定的規則：自檢程度低就不該放進去）。
                "sections": [] if r["needs_review"] else (g.get("sections") or []),
                "stats": [] if r["needs_review"] else (g.get("stats") or []),
                "delta_insight": None if r["needs_review"] else g.get("delta_insight"),
            }
        )

    tldr = None
    try:
        if issue["tldr_json"]:
            tldr = json.loads(issue["tldr_json"])
    except (KeyError, IndexError):
        pass

    # 主要報導排前面（NBDMD 清單來源或主角是 watchlist 廠商），其餘進額外
    # 報導。兩區各自維持原本的排序（選題分數高到低）。
    primary_articles = [a for a in articles if a["is_primary"]]
    extra_articles = [a for a in articles if not a["is_primary"]]

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    html = env.get_template("email_issue.html.jinja").render(
        tldr=tldr,
        tldr_groups=tldr_display_groups(tldr),
        editorial_sections=_with_emphasis((tldr or {}).get("editorial_sections")),
        newsletter_name=config["newsletter"]["name"],
        issue_title=f"{config['newsletter']['name']}（{issue['issue_date']}）",
        issue_date=issue["issue_date"],
        issue_no=issue["id"],
        articles=articles,
        primary_articles=primary_articles,
        extra_articles=extra_articles,
        site_url=SITE_URL,
    )

    out = ROOT / "runs" / f"email_preview_{issue['issue_date']}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[render_issue_email] 已輸出：{out}（{len(articles)} 則）")


if __name__ == "__main__":
    main()
