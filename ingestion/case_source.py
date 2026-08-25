"""從企業案例類 RSS 來源（如廠商官方 Customer Stories blog）抓取近期文章，
作為 Delta Pulse 案例式週報的候選內容。

跟 arxiv_source.py / reddit_source.py 是平行的來源模組，一樣輸出 RawItem，
後面的去重/評分/生成流程不用為這個來源另外寫邏輯。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

from ingestion.base import RawItem


def _entry_html(entry: dict) -> str:
    """有些來源（實測過 NVIDIA Blog、Google Cloud Blog）的 RSS content
    欄位存在但 value 是空字串（不是欄位缺失，是那個欄位本身沒填內容），
    這種情況要退回 summary，不然這篇文章會因為「有 content 就直接採用」
    被判定成沒有正文、整篇跳過。"""
    content_list = entry.get("content")
    if content_list:
        value = content_list[0].get("value", "")
        if value.strip():
            return value
    return entry.get("summary", "") or entry.get("description", "")


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


def fetch_case_study_items(
    source_id: str,
    source_name: str,
    url: str,
    weight: float,
    days_back: int = 30,
    max_items: int = 15,
    timeout: int = 15,
) -> list[RawItem]:
    """抓一個案例來源的 RSS feed，轉成 RawItem 清單。

    RawItem.summary 放全文（HTML 已轉純文字，未截斷），截斷交給後面組
    prompt 時再做，避免這一層就把可能有用的事實砍掉。
    RawItem.extra 帶 source_name/source_weight，供評分/生成階段組 prompt 用。
    RawItem.score 直接用來源權重，方便沿用 legacy/pipeline/dedupe.py 既有的
    「同網址留分數較高那筆」邏輯。

    先用 requests 帶 timeout 抓內容，再交給 feedparser 解析字串，不能直接把
    url 丟給 feedparser.parse()：那個寫法底層是用 urllib 開連線，不接受
    timeout 參數，遇到回應很慢或掛住的來源會讓整支 pipeline 卡死。
    """
    response = requests.get(
        url, timeout=timeout, headers={"User-Agent": "delta-ai-newsletter/0.1"}
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    items: list[RawItem] = []
    for entry in feed.entries[:max_items]:
        parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
        published_at = (
            datetime(*parsed_time[:6], tzinfo=timezone.utc) if parsed_time else None
        )
        if published_at is not None and published_at < cutoff:
            continue

        text = _html_to_text(_entry_html(entry))
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not text or not link:
            continue

        items.append(
            RawItem(
                title=title,
                url=link,
                source="case_study",
                subdomain_id=source_id,
                published_at=published_at or datetime.now(timezone.utc),
                summary=text,
                score=weight,
                extra={"source_name": source_name, "source_weight": weight},
            )
        )
    return items
