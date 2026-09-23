"""不出刊日的速報信（2026-09-23 加）。

日報改成「夠份量才出」之後會有不出刊的日子（9/20、9/23 各一次）。那些天
讀者信箱是空的，而空信箱分不出兩件事：今天沒有重點動態，還是排程壞了。
沉默比內容不足更傷信任。

這支把那天改成一封速報：列出近期被最多家媒體同時報導的話題，只給標題、
幾家在報、報導它的媒體與原文連結。刻意不生成摘要也不做查核，因為沒有
經過我們平常那套關卡的內容不該用一樣的口吻呈現，信尾也講明了這件事。

用法：
    python -m tools.render_no_issue_email                  # 今天
    python -m tools.render_no_issue_email --date 2026-09-23
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.topic_db import get_connection
from pipeline.topic_selection import compute_hotness

ROOT = Path(__file__).resolve().parent.parent

# 速報最多列幾則。這封信的定位是「快速掃一眼」，列太多就變成另一種日報。
_MAX_TOPICS = 6
# 至少要有這麼多家在報才列。只有一家報的東西還不算「正在被討論」，
# 放進速報會讓這封信看起來只是把落選的東西倒出來。
_MIN_SOURCES = 2


def hot_topics(conn, config: dict, as_of: date, limit: int = _MAX_TOPICS) -> list[dict]:
    half_life = config["hotness"]["half_life_days"]
    window = config["hotness"]["window_days"]
    since = (as_of - timedelta(days=window)).isoformat()
    now = datetime.now(timezone.utc)

    rows = conn.execute(
        """SELECT DISTINCT t.id, t.representative_title FROM topics t
           JOIN articles a ON a.topic_id = t.id
           WHERE a.discarded_at IS NULL AND a.gate_status != 'excluded'
             AND date(a.published_at) BETWEEN ? AND ?""",
        (since, as_of.isoformat()),
    ).fetchall()

    entries = []
    for row in rows:
        articles = conn.execute(
            """SELECT source_id, source_name, source_weight, published_at, title, url
               FROM articles WHERE topic_id = ? AND discarded_at IS NULL
                 AND gate_status != 'excluded'
               ORDER BY published_at DESC""",
            (row["id"],),
        ).fetchall()
        if not articles:
            continue
        sources = sorted({a["source_name"] for a in articles})
        if len(sources) < _MIN_SOURCES:
            continue
        entries.append(
            {
                "title": row["representative_title"],
                "hotness": compute_hotness(articles, now, half_life),
                "source_count": len(sources),
                "sources": sources,
                "url": articles[0]["url"],
            }
        )
    entries.sort(key=lambda e: e["hotness"], reverse=True)
    return entries[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()
    as_of = date.fromisoformat(args.date)

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))

    existing = conn.execute(
        "SELECT id FROM issues WHERE issue_date = ? AND cadence = 'daily'", (args.date,)
    ).fetchone()
    if existing:
        print(f"[render_no_issue_email] {args.date} 已經有第 {existing['id']} 期，不需要速報信。")
        return

    topics = hot_topics(conn, config, as_of)
    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    html = env.get_template("email_no_issue.html.jinja").render(
        newsletter_name=config["newsletter"]["name"],
        issue_title=f"{config['newsletter']['name']}　今日速報（{args.date}）",
        issue_date=args.date,
        hot_topics=topics,
    )
    out = ROOT / "runs" / f"email_no_issue_{args.date}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[render_no_issue_email] 已輸出：{out}（{len(topics)} 則速報）")


if __name__ == "__main__":
    main()
