"""從企業案例類 RSS 來源（如廠商官方 Customer Stories blog）抓取近期文章，
作為 Delta Pulse 案例式週報的候選內容。

跟 arxiv_source.py / reddit_source.py 是平行的來源模組，一樣輸出 RawItem，
後面的去重/評分/生成流程不用為這個來源另外寫邏輯。
"""
from __future__ import annotations

import time
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


# 全文抓取用的 UA：有些站對非瀏覽器 UA 直接擋。
_FULLTEXT_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# feed 也要用瀏覽器 UA（2026-10-05 查出來的）：Endpoints News 接上之後一則都
# 沒抓到，實測同一個網址用 "delta-ai-newsletter/0.1" 回 403、用瀏覽器 UA 回
# 200 且有 24 篇。全文抓取早就為同樣的理由改過 UA，feed 這層漏掉了。
_FEED_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}


def _get_html(url: str, timeout: int) -> str:
    """取回頁面 HTML。普通請求被擋時改用模擬瀏覽器指紋的管道再試一次。

    2026-10-05 加這條退路：MedTech Dive 的文章頁對任何 User-Agent 都回 403，
    因為它認的是 TLS 握手的指紋而不是 UA。curl_cffi 模擬 Chrome 的指紋之後
    同樣的網址回 200、正文 3,400 到 4,100 字（實測三篇）。卡在這一關的正是
    Abbott 推出 Freenome 大腸癌檢測那種我們最想要的廠商動態。

    只在第一條路失敗時才走第二條，正常來源不受影響。
    """
    try:
        response = requests.get(url, headers=_FULLTEXT_UA, timeout=timeout)
        if response.status_code == 200:
            return response.text
    except Exception:  # noqa: BLE001 -- 換下一條路
        pass
    try:
        from curl_cffi import requests as cffi_requests

        alt = cffi_requests.get(url, impersonate="chrome", timeout=timeout + 10)
        if alt.status_code == 200:
            return alt.text
    except Exception:  # noqa: BLE001 -- 兩條都不行就沿用 feed 摘要
        pass
    return ""


def _fetch_fulltext(url: str, timeout: int = 20) -> str:
    """照文章連結抓原文正文（genomics-prototype 2026-08-31 加）。

    GeneOnline、Fierce Biotech、BioSpace 這些來源的 feed 只給摘要，
    轉純文字後不到收錄門檻的 200 字，全部被降成 signal_only、當不了
    寫作素材。開了 fetch_fulltext 的來源會在摘要太短時走這條路補全文。
    抽取用簡單的啟發式（article/main 標籤，去掉導覽頁尾），抓不到或
    失敗就回空字串、沿用 feed 摘要，單篇失敗不影響整批。"""
    html = _get_html(url, timeout)
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        node = soup.find("article") or soup.find("main") or soup.body
        if node is None:
            return ""
        # 有的頁面抓不到正文節點時會退到 body，撈回整站的導覽與列表
        # （實測 BioSpace 一頁 11 萬字）。正常新聞正文遠小於 2 萬字，
        # 超過的直接截斷，保護後面的 embedding 與 LLM 呼叫。
        return node.get_text(separator="\n", strip=True)[:20_000]
    except Exception:  # noqa: BLE001 -- 抓不到就退回 feed 摘要
        return ""


def fetch_case_study_items(
    source_id: str,
    source_name: str,
    url: str,
    weight: float,
    days_back: int = 30,
    max_items: int = 15,
    timeout: int = 15,
    fetch_fulltext: bool = False,
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
    response = requests.get(url, timeout=timeout, headers=_FEED_UA)
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
        # 有的來源連 title 欄位都塞 HTML（實測 Fierce Biotech 的 title 是
        # 整個 <a href=...> 標籤），一律過一次轉純文字。
        title = _html_to_text(entry.get("title", "")).replace("\n", " ").strip()
        link = entry.get("link", "").strip()
        if not link:
            continue
        # feed 摘要太短時去抓原文（600 是「明顯只是摘要」的經驗值，
        # 高於收錄門檻的 200，給正常短文留餘地）。
        if fetch_fulltext and len(text) < 600:
            fulltext = _fetch_fulltext(link)
            if len(fulltext) > len(text):
                text = fulltext
            time.sleep(0.5)  # 對站方客氣一點
        if not text:
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
