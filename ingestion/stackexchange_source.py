"""從 Stack Exchange 抓取指定 tag 的近期問題，作為開發者實務踩雷/做法的訊號來源。

用 Stack Exchange 官方公開 API，不需金鑰（有 IP 層級的每日配額，量不大不會
超）：https://api.stackexchange.com/docs

跟 hn_source.py 是平行模組，一樣輸出 RawItem。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from ingestion.base import RawItem

STACKEXCHANGE_API_URL = "https://api.stackexchange.com/2.3/questions"


def fetch_stackexchange_items(
    source_id: str,
    source_name: str,
    tags: list[str],
    weight: float,
    site: str = "stackoverflow",
    min_score: int = 1,
    days_back: int = 30,
    max_items: int = 20,
    timeout: int = 15,
) -> list[RawItem]:
    """依 tag 清單分別查詢近期問題，過濾掉分數太低（雜訊）的貼文再合併去重。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    seen_ids: set[int] = set()
    items: list[RawItem] = []
    for tag in tags:
        params = {
            "order": "desc",
            # 一定要用 creation 不能用 activity：下面的 cutoff 比的是
            # creation_date，sort=activity 排的卻是「最後有人回答/編輯的時間」，
            # 兩個欄位不一致的話 API 會忠實回傳「幾年前發問、最近有人推文」的
            # 老問題，再被 cutoff 全部砍光，整個來源穩定回傳 0 筆卻不報錯。
            # 2026-08-10 實測：sort=activity 時 artificial-intelligence 這個 tag
            # 回傳 15 筆的最新「建立」時間是 2026-01-04（七個月前），全滅。
            "sort": "creation",
            "tagged": tag,
            "site": site,
            "pagesize": max_items,
            "filter": "!9_bDDxJY5",  # 官方預設 filter 之一，含 body（HTML）
        }
        response = requests.get(STACKEXCHANGE_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
        for q in response.json().get("items", []):
            if q["question_id"] in seen_ids:
                continue
            seen_ids.add(q["question_id"])

            created_at = datetime.fromtimestamp(q["creation_date"], tz=timezone.utc)
            if created_at < cutoff or q.get("score", 0) < min_score:
                continue

            body_html = q.get("body", "") or ""
            body_text = BeautifulSoup(body_html, "html.parser").get_text(separator="\n", strip=True)
            items.append(
                RawItem(
                    title=q["title"],
                    url=q["link"],
                    source="stackexchange",
                    subdomain_id=source_id,
                    published_at=created_at,
                    summary=body_text[:1500],
                    score=float(q.get("score", 0)),
                    extra={
                        "source_name": source_name,
                        "source_weight": weight,
                        "tag": tag,
                        "answer_count": q.get("answer_count", 0),
                    },
                )
            )
    return items[:max_items]
