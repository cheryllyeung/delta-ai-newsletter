"""列表頁爬蟲來源：對沒有 RSS 的網站，抓列表頁挖文章連結，再抓各篇全文。

genomics-prototype 2026-09-02 加，補台灣內容供給（環球生技、Genet 觀點
都沒有公開 RSS，但列表頁是靜態 HTML，可爬）。每個站的差異（連結格式、
日期與內文的抽取）用 config 的參數描述，不寫死在程式裡：

  type: scrape
  list_url: 列表頁網址
  link_pattern: 文章連結的正規式（在列表頁的 <a href> 上比對）
  base_url: 相對連結補成絕對網址用的前綴
  content_selector: 文章頁內文節點的 CSS class 關鍵字（選填，找不到退 article/main/body）

抓到的全文一律走既有的收錄判定與標籤流程，跟 RSS 來源沒有差別。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from ingestion.base import RawItem

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_DATE_RE = re.compile(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})")


def _extract_date(html: str) -> datetime | None:
    m = _DATE_RE.search(html)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_body(soup: BeautifulSoup, content_selector: str | None) -> str:
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    node = None
    if content_selector:
        node = soup.find(class_=re.compile(content_selector, re.I))
    node = node or soup.find("article") or soup.find("main") or soup.body
    if node is None:
        return ""
    return node.get_text(separator="\n", strip=True)[:20_000]


def fetch_scraped_items(
    source_id: str,
    source_name: str,
    weight: float,
    list_url: str,
    link_pattern: str,
    base_url: str,
    content_selector: str | None = None,
    days_back: int = 30,
    max_items: int = 15,
    timeout: int = 20,
) -> list[RawItem]:
    """抓列表頁的文章連結，逐篇抓全文，回傳 RawItem 清單。"""
    resp = requests.get(list_url, headers=_UA, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    pat = re.compile(link_pattern)
    seen_urls: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=pat):
        title = a.get_text(strip=True)
        if len(title) < 10:
            continue
        href = a["href"]
        url = href if href.startswith("http") else base_url.rstrip("/") + "/" + href.lstrip("/")
        seen_urls.append((url, title))
    # 去重，保序
    seen_urls = list(dict.fromkeys(seen_urls))[:max_items]

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[RawItem] = []
    for url, title in seen_urls:
        try:
            art = requests.get(url, headers=_UA, timeout=timeout)
            art.raise_for_status()
        except Exception as exc:  # noqa: BLE001 -- 單篇失敗不影響整批
            print(f"[scrape_source]   {source_name} 抓取單篇失敗，跳過：{exc}")
            continue
        asoup = BeautifulSoup(art.text, "html.parser")
        published_at = _extract_date(art.text) or datetime.now(timezone.utc)
        if published_at < cutoff:
            continue
        body = _extract_body(asoup, content_selector)
        if not body:
            continue
        items.append(
            RawItem(
                title=title,
                url=url,
                source="scrape",
                subdomain_id=source_id,
                published_at=published_at,
                summary=body,
                score=weight,
                extra={"source_name": source_name, "source_weight": weight},
            )
        )
        time.sleep(0.5)  # 對站方客氣一點
    return items
