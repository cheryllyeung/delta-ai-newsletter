"""從 Hacker News 抓取 AI 相關討論串，作為早期訊號偵測的訊號層來源。

用 Algolia 的公開搜尋 API（HN 官方合作維護，不需金鑰）：
https://hn.algolia.com/api

跟 arxiv_source.py 一樣輸出 RawItem，不用為這個來源另外寫去重/評分邏輯。
HN 貼文本身多半只是連結加標題，沒有全文，summary 用標題加討論熱度合成，
定位是「早期訊號」不是完整寫作素材，跟 PRD 對訊號層的定義一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ingestion.base import RawItem

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"


def fetch_hn_items(
    source_id: str,
    source_name: str,
    queries: list[str],
    weight: float,
    min_points: int = 20,
    days_back: int = 7,
    max_items: int = 20,
    timeout: int = 15,
) -> list[RawItem]:
    """依關鍵字清單分別搜尋近期 story，過濾掉點數太低（雜訊）的貼文。

    Algolia 的 query 參數是全文比對，不是布林查詢語法，"A OR B" 會被當成
    字面文字比對，不會真的展開成「A 或 B」。要涵蓋多個關鍵字，要分開
    查詢再合併去重，不能塞一個 "A OR B OR C" 的字串進去。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    seen_ids: set[str] = set()
    items: list[RawItem] = []
    for query in queries:
        params = {
            "query": query,
            "tags": "story",
            "hitsPerPage": max_items,
            "numericFilters": f"created_at_i>{int(cutoff.timestamp())},points>={min_points}",
        }
        response = requests.get(HN_SEARCH_URL, params=params, timeout=timeout)
        response.raise_for_status()
        for hit in response.json().get("hits", []):
            if hit["objectID"] in seen_ids:
                continue
            seen_ids.add(hit["objectID"])

            title = (hit.get("title") or "").strip()
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
            if not title:
                continue
            points = hit.get("points", 0)
            num_comments = hit.get("num_comments", 0)
            items.append(
                RawItem(
                    title=title,
                    url=url,
                    source="hackernews",
                    subdomain_id=source_id,
                    published_at=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
                    summary=f"Hacker News 討論：{title}（{points} 點，{num_comments} 則留言）",
                    score=float(points),
                    extra={
                        "source_name": source_name,
                        "source_weight": weight,
                        "hn_discussion_url": f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    },
                )
            )
    return items[:max_items]
