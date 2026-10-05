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

# 2026-10-05 換成完整的瀏覽器 UA：短字串在某些站拿到的頁面跟瀏覽器看到的
# 不一樣（Element Biosciences 與 Sophia Genetics 實測一篇都挖不到），
# case_source.py 也為同樣理由改過。
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_DATE_RE = re.compile(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})")


# 結構化的發布時間欄位，照可信度排序。2026-10-05 加：原本只用正規式掃整頁
# 的第一個日期，很容易抓到版權年份或指令碼裡的舊日期，整批文章就被當成過期
# 丟掉（Sophia Genetics 實測樣式命中 20 個連結卻一篇都沒收）。
_Q = "[\"']"  # 屬性值的引號，單雙引號都要吃
_META_DATE = [
    re.compile(rf"<meta[^>]+property={_Q}article:published_time{_Q}[^>]+content={_Q}([^\"']+)", re.I),
    re.compile(rf"<meta[^>]+name={_Q}(?:pubdate|publishdate|date){_Q}[^>]+content={_Q}([^\"']+)", re.I),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
    re.compile(rf"<time[^>]+datetime={_Q}([^\"']+)", re.I),
]


def _extract_date(html: str) -> datetime | None:
    for pattern in _META_DATE:
        m = pattern.search(html)
        if not m:
            continue
        found = _DATE_RE.search(m.group(1))
        if found:
            try:
                return datetime(
                    int(found.group(1)), int(found.group(2)), int(found.group(3)), tzinfo=timezone.utc
                )
            except ValueError:
                pass
    m = _DATE_RE.search(html)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_body(soup: BeautifulSoup, content_selector: str | None) -> str:
    # 2026-10-05 不再整個拆掉 <form>：ASP.NET WebForms 會把整個版面包在一個
    # form 裡，拆掉等於把內容刪光（Thermo Fisher 的發布頁實測只剩 46 字，
    # 一度誤判成渲染等待不夠）。改成只拆表單控制項。
    for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                     "input", "select", "textarea", "button"]):
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
    render: bool = False,
) -> list[RawItem]:
    """抓列表頁的文章連結，逐篇抓全文，回傳 RawItem 清單。

    render=True 的來源用無頭瀏覽器取頁面（見 ingestion/render.py）。只有少數
    站需要（實測只有 Thermo Fisher 的發布頁），所以預設關閉。
    """
    # render=True 的來源用同一個瀏覽器處理列表頁與所有文章頁（見
    # ingestion/render.py：每頁重開一次會逾時，實測正文只剩 46 字）。
    renderer = None
    if render:
        from ingestion.render import Renderer

        renderer = Renderer().__enter__()
    try:
        html = renderer(list_url) if renderer else ""
        if not html:
            resp = requests.get(list_url, headers=_UA, timeout=timeout)
            resp.raise_for_status()
            html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        # 2026-10-05：先拆掉導覽與頁首頁尾再挖連結。選單裡常有符合樣式的連結，
        # 而取用是照文件順序取前 max_items 篇，結果整批都是選單
        # （Myriad 實測抓到「About Myriad Genetics」這種頁面）。
        for tag in soup(["nav", "header", "footer", "aside", "script", "style"]):
            tag.decompose()

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
            art_html = renderer(url) if renderer else ""
            if not art_html:
                try:
                    art = requests.get(url, headers=_UA, timeout=timeout)
                    art.raise_for_status()
                    art_html = art.text
                except Exception as exc:  # noqa: BLE001 -- 單篇失敗不影響整批
                    print(f"[scrape_source]   {source_name} 抓取單篇失敗，跳過：{exc}")
                    continue
            asoup = BeautifulSoup(art_html, "html.parser")
            published_at = _extract_date(art_html) or datetime.now(timezone.utc)
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
    finally:
        if renderer is not None:
            renderer.__exit__(None, None, None)
    return items
