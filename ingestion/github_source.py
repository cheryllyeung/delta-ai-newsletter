"""從 GitHub 抓取近期新建立、依 star 數排序的相關 repo，作為開發者工具/
框架動態的訊號來源。

用 GitHub 官方 Search API，不需金鑰（未認證每小時 10 次請求的配額，
每日排程跑一次用量不大）：https://docs.github.com/en/rest/search
若有設定 GITHUB_TOKEN 環境變數會自動帶上，配額會提高到每小時 30 次。

跟 hn_source.py / stackexchange_source.py 是平行模組，一樣輸出 RawItem。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

from ingestion.base import RawItem

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"


def fetch_github_items(
    source_id: str,
    source_name: str,
    queries: list[str],
    weight: float,
    min_stars: int = 20,
    days_back: int = 14,
    max_items: int = 20,
    timeout: int = 15,
) -> list[RawItem]:
    """依查詢字串清單分別搜尋近期建立的 repo，過濾掉 star 數太低的雜訊。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    seen_ids: set[int] = set()
    items: list[RawItem] = []
    for query in queries:
        params = {
            "q": f"{query} created:>{cutoff.date().isoformat()} stars:>={min_stars}",
            "sort": "stars",
            "order": "desc",
            "per_page": max_items,
        }
        response = requests.get(GITHUB_SEARCH_URL, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        for repo in response.json().get("items", []):
            if repo["id"] in seen_ids:
                continue
            seen_ids.add(repo["id"])

            description = repo.get("description") or ""
            items.append(
                RawItem(
                    title=repo["full_name"],
                    url=repo["html_url"],
                    source="github",
                    subdomain_id=source_id,
                    published_at=datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00")),
                    summary=f"{description}（{repo['stargazers_count']} stars，語言：{repo.get('language') or '未標示'}）",
                    score=float(repo["stargazers_count"]),
                    extra={"source_name": source_name, "source_weight": weight},
                )
            )
    return items[:max_items]
