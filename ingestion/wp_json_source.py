"""WordPress REST API 來源（2026-10-05 加）。

為什麼需要這條路：好幾家廠商的網站是 WordPress，但三種抓法都有缺陷。

- 預設 feed 常年久失修（Sophia Genetics 的 /feed/ 最新停在 2023 年）
- 新聞放在自訂文章類型下，列表頁又靠 JavaScript 載入，靜態爬蟲看不到
  （Sophia 的靜態頁最新只到 6 月初，實際上 8 月還在發）
- feed 即使對了也只有摘要

WordPress 本身提供 /wp-json/wp/v2/<type> 這個介面，直接回 JSON 且帶全文。
實測 Sophia 的 news 類型拿到 54,083 字、Caris 的 press_release 拿到 8,654 字，
都比 feed 完整。這條路只用 requests，不需要瀏覽器或新套件。

設定寫法：

  type: wp_json
  api_url: "https://www.example.com/wp-json/wp/v2/news"
  weight: 0.8

要知道某個站有哪些類型可用，打 /wp-json/wp/v2/types 看 rest_base。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from ingestion.base import RawItem

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _text(html: str) -> str:
    """WordPress 回的是 HTML 片段，轉成純文字再交給後面的流程。"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)[:20_000]


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", _text(html)).strip()


def fetch_wp_json_items(
    source_id: str,
    source_name: str,
    weight: float,
    api_url: str,
    days_back: int = 30,
    max_items: int = 15,
    timeout: int = 20,
) -> list[RawItem]:
    """抓 WordPress REST API 的文章，回傳 RawItem 清單。"""
    sep = "&" if "?" in api_url else "?"
    resp = requests.get(f"{api_url}{sep}per_page={max_items}", headers=_UA, timeout=timeout)
    resp.raise_for_status()
    posts = resp.json()
    if not isinstance(posts, list):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[RawItem] = []
    for post in posts:
        raw_date = (post.get("date_gmt") or post.get("date") or "")[:19]
        try:
            published_at = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
        except ValueError:
            published_at = datetime.now(timezone.utc)
        if published_at < cutoff:
            continue
        body = _text((post.get("content") or {}).get("rendered", ""))
        if not body:
            continue
        items.append(
            RawItem(
                title=_plain((post.get("title") or {}).get("rendered", "")),
                url=post.get("link") or api_url,
                source="wp_json",
                subdomain_id=source_id,
                published_at=published_at,
                summary=body,
                score=weight,
                extra={"source_name": source_name, "source_weight": weight},
            )
        )
    return items
