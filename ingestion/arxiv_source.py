"""從 arXiv API 依分類抓取近期論文，作為各領域的學術面內容來源。

arXiv API 為公開資料、不需金鑰，詳見 https://info.arxiv.org/help/api/index.html
注意：官方請求頻率限制為 3 秒一次請求，本模組已內建節流。
"""
from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from ingestion.base import RawItem

ARXIV_API_URL = "https://export.arxiv.org/api/query"
_MIN_REQUEST_INTERVAL_SECONDS = 3.0
_last_request_at: float = 0.0


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def fetch_arxiv_items(
    subdomain_id: str,
    categories: list[str],
    keywords: list[str],
    max_results: int = 20,
    days_back: int = 14,
    timeout: int = 15,
) -> list[RawItem]:
    """抓取指定分類的近期 arXiv 論文，並依關鍵字加權排序。

    回傳的 RawItem.score 是關鍵字命中數（0 也會回傳，交給後續流程篩選/排序）。
    """
    if not categories:
        return []

    cat_query = " OR ".join(f"cat:{c}" for c in categories)
    query = urllib.parse.urlencode(
        {
            "search_query": cat_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        }
    )

    _throttle()
    response = requests.get(f"{ARXIV_API_URL}?{query}", timeout=timeout)
    response.raise_for_status()

    feed = feedparser.parse(response.text)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    keywords_lower = [k.lower() for k in keywords]

    items: list[RawItem] = []
    for entry in feed.entries:
        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if published_at < cutoff:
            continue

        title = entry.title.replace("\n", " ").strip()
        summary = entry.summary.replace("\n", " ").strip()
        haystack = f"{title} {summary}".lower()
        keyword_hits = sum(1 for k in keywords_lower if k in haystack)

        items.append(
            RawItem(
                title=title,
                url=entry.link,
                source="arxiv",
                subdomain_id=subdomain_id,
                published_at=published_at,
                summary=summary,
                score=float(keyword_hits),
                extra={"arxiv_categories": categories},
            )
        )
    return items
