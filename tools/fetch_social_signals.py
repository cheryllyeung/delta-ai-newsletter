"""抓社群討論與留言，給速報信用（2026-09-23 加）。

用途限定在「當天品質不足以出一期日報」時的話題速報
（tools/render_no_issue_email.py）。抓回來的是討論串標題加留言、沒有實質
內文，入池時就被收錄判定降級成 signal_only，不當寫作素材也不打分，所以
不會影響正刊。

抓 Hacker News 的 Algolia 介面（公開免金鑰）。Reddit 要 API 金鑰，.env
目前沒設，等哪天申請了再加進來。

留言另存一張 social_comments 表，速報的「大家怎麼看」直接引述它，只做
翻譯不做改寫（使用者 2026-09-23 定：直接引述大家講的加翻譯就好）。

用法：
    python -m tools.fetch_social_signals
    python -m tools.fetch_social_signals --days 7
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.base import RawItem
from pipeline.topic_db import get_connection, insert_article_if_new

ROOT = Path(__file__).resolve().parent.parent
SEARCH_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{}"
SOURCE_ID = "hn_genomics"
SOURCE_NAME = "Hacker News（討論）"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS social_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    author TEXT,
    text_original TEXT NOT NULL,
    text_zh TEXT,
    points INTEGER,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_comments_article ON social_comments(article_id);
"""

# 留言長度界線：太短的多半是「+1」「same here」，太長的引述起來會佔掉
# 整封信，兩邊都不收。
_MIN_COMMENT_CHARS = 60
_MAX_COMMENT_CHARS = 400


def _strip_html(text: str) -> str:
    import html
    import re

    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def fetch_top_comments(story_id: str, limit: int) -> list[dict]:
    """抓一則討論串的前幾則留言。Algolia 的 items 端點回整棵樹，只取第一層。"""
    try:
        resp = requests.get(ITEM_URL.format(story_id), timeout=20,
                            headers={"User-Agent": "delta-genomics/1.0"})
        resp.raise_for_status()
        children = resp.json().get("children") or []
    except Exception as exc:  # noqa: BLE001 -- 留言抓不到不影響主體
        print(f"[fetch_social]   留言抓取失敗（{story_id}）：{str(exc)[:60]}")
        return []

    out = []
    for c in children:
        text = _strip_html(c.get("text") or "")
        if not (_MIN_COMMENT_CHARS <= len(text) <= _MAX_COMMENT_CHARS):
            continue
        out.append({"author": c.get("author"), "text": text, "points": c.get("points") or 0})
        if len(out) >= limit:
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="只收這幾天內的討論（預設 14）")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    cfg = config["speed_report"]
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))
    conn.executescript(_SCHEMA)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    inserted = comments_saved = 0

    for query in cfg["social_queries"]:
        try:
            resp = requests.get(
                SEARCH_URL,
                params={"query": query, "tags": "story", "hitsPerPage": 20},
                timeout=20,
                headers={"User-Agent": "delta-genomics/1.0"},
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:  # noqa: BLE001 -- 單一關鍵詞失敗不中斷整批
            print(f"[fetch_social] 搜尋失敗（{query}）：{str(exc)[:70]}")
            continue

        kept = 0
        for h in hits:
            title = (h.get("title") or "").strip()
            points = h.get("points") or 0
            if not title or points < cfg["min_points"]:
                continue
            try:
                published = datetime.fromisoformat((h.get("created_at") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if published < cutoff:
                continue
            story_id = h.get("objectID")
            url = h.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
            item = RawItem(
                title=title,
                url=url,
                source=SOURCE_ID,
                # insert_article_if_new() 的 source_id 讀的是 subdomain_id。
                subdomain_id=SOURCE_ID,
                published_at=published,
                # 內文刻意留空：這種東西的價值是訊號不是內文，空內容也會讓
                # 收錄判定自動把它降成 signal_only，不會混進寫作素材。
                summary="",
                score=float(points),
                extra={
                    "source_name": SOURCE_NAME,
                    "source_weight": 0.5,
                    "tier": "signal",
                    "engagement_metric": "hn_points",
                },
            )
            article_id = insert_article_if_new(conn, item)
            if not article_id:
                continue
            inserted += 1
            kept += 1
            for c in fetch_top_comments(story_id, cfg["max_quotes"]):
                conn.execute(
                    """INSERT INTO social_comments
                       (article_id, author, text_original, text_zh, points, fetched_at)
                       VALUES (?, ?, ?, NULL, ?, ?)""",
                    (article_id, c["author"], c["text"], c["points"],
                     datetime.now().isoformat()),
                )
                comments_saved += 1
            time.sleep(0.3)
        print(f"[fetch_social] 「{query}」：新增 {kept} 則討論")
        time.sleep(0.3)

    conn.commit()
    print(f"[fetch_social] 完成：新增 {inserted} 則討論、{comments_saved} 則留言。")
    print("[fetch_social] 這些是 signal 層，只給速報信用，不進正刊。")


if __name__ == "__main__":
    main()
