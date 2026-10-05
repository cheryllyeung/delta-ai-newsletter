"""探測名單廠商的新聞列表頁，推斷爬取設定（2026-10-05 加）。

為什麼不用 RSS：實測 26 家裡只有 8 家有活著的 feed，而且像 Sophia Genetics
那樣預設 feed 停在 2023 年、真正的新聞在另一個文章類型下。使用者決定全部
改成直接爬網站，所以需要每家的列表頁網址與文章連結樣式。

這支做的事：對每個網域試常見的新聞列表路徑，從頁面挖出看起來像文章的連結，
推斷連結樣式，再抓第一篇試抽內文，最後印出可以直接貼進 config 的設定區塊。
推斷不保證對，所以每家都印出樣本標題與內文長度，由人看過再收。

只讀網路、不碰資料庫。

用法：
    python -m tools.probe_vendor_pages
    python -m tools.probe_vendor_pages --only Natera,Tempus AI
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.scrape_source import _extract_body
from tools.probe_vendor_feeds import UA, VENDOR_DOMAINS

# 常見的新聞列表路徑，照命中率排序
LIST_PATHS = [
    "/news",
    "/news-room",
    "/newsroom",
    "/press-releases",
    "/press",
    "/media",
    "/company/news",
    "/about/news",
    "/about-us/news",
    "/resources/news",
    "/investors/news",
    "/news-events/news-releases",
    "/blog",
    "/insights",
]
# 列表頁上明顯不是文章的連結
SKIP = re.compile(
    r"(/category/|/tag/|/author/|/page/|/wp-|\.pdf$|\.jpg$|\.png$|/privacy|/terms|/cookie"
    r"|/careers|/contact|/login|/search|facebook|twitter|linkedin|youtube|instagram)",
    re.I,
)


def _article_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    # 2026-10-05：先拆掉導覽與頁首頁尾。原本連這些一起挖，結果推斷出來的
    # 是產品分類路徑（Guardant 抓到 /precision-oncology/、Natera 抓到
    # /organ-health/），不是新聞。
    for tag in soup(["nav", "header", "footer", "aside", "script", "style"]):
        tag.decompose()
    host = urlparse(base).netloc.replace("www.", "")
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"]).split("#")[0].split("?")[0]
        if urlparse(url).netloc.replace("www.", "") != host or SKIP.search(url):
            continue
        path = urlparse(url).path.rstrip("/")
        # 文章網址的特徵：路徑有兩層以上，而且最後一段像標題（有連字號或夠長）
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            continue
        slug = parts[-1]
        if "-" in slug or len(slug) > 18 or slug.isdigit():
            out.append(url)
    return list(dict.fromkeys(out))


# 像新聞的路徑段。推斷時優先採用這些，其次才看出現次數
_NEWSY = re.compile(r"^(news|news-room|newsroom|press|press-releases|media|announcements|"
                    r"insights|blog|stories|resources|20\d\d)$", re.I)


def _infer_pattern(links: list[str], list_url: str) -> str | None:
    """從連結推斷正規式，例如 /news/<slug>。

    2026-10-05 改法：先看列表頁自己的路徑段（/newsroom/ 底下的文章通常也在
    /newsroom/ 或 /news/），再看像新聞的字樣，最後才退回出現次數。只看次數
    會挑到導覽列的產品分類。
    """
    firsts = Counter()
    for url in links:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) >= 2:
            firsts[parts[0]] += 1
    if not firsts:
        return None

    list_parts = [p for p in urlparse(list_url).path.split("/") if p]
    preferred = [p for p in list_parts if p in firsts]
    newsy = [seg for seg in firsts if _NEWSY.match(seg)]
    for candidate in preferred + sorted(newsy, key=lambda s: -firsts[s]):
        if firsts[candidate] >= 2:
            return rf"/{re.escape(candidate)}/[\w%-]+"

    top, count = firsts.most_common(1)[0]
    if count < 4 or not _NEWSY.match(top):
        return None
    return rf"/{re.escape(top)}/[\w%-]+"


def probe(vendor: str, domain: str) -> dict | None:
    for host in (f"https://www.{domain}", f"https://{domain}"):
        for path in LIST_PATHS:
            list_url = host + path
            try:
                r = requests.get(list_url, timeout=12, headers=UA, allow_redirects=True)
            except Exception:  # noqa: BLE001 -- 試不到就換下一個
                continue
            if r.status_code != 200 or "<html" not in r.text[:3000].lower():
                continue
            links = _article_links(r.text, r.url)
            pattern = _infer_pattern(links, r.url)
            if not pattern or len(links) < 3:
                continue
            matched = [u for u in links if re.search(pattern, u)]
            sample_title, body_len = "", 0
            if matched:
                try:
                    a = requests.get(matched[0], timeout=15, headers=UA)
                    soup = BeautifulSoup(a.text, "html.parser")
                    sample_title = (soup.title.get_text(strip=True) if soup.title else "")[:70]
                    body_len = len(_extract_body(soup, None))
                except Exception:  # noqa: BLE001
                    pass
            return {
                "vendor": vendor,
                "list_url": r.url,
                "pattern": pattern,
                "links": len(matched),
                "sample": sample_title,
                "body_len": body_len,
                "base_url": host,
            }
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    targets = {k: v for k, v in VENDOR_DOMAINS.items() if v}
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        targets = {k: v for k, v in targets.items() if k in wanted}

    print(f"[probe] 測 {len(targets)} 家的新聞列表頁\n")
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(lambda kv: probe(*kv), targets.items()):
            if res:
                results.append(res)
                print(f"  {res['vendor']:22} {res['list_url']}")
                print(f"  {'':22} 樣式 {res['pattern']}　文章連結 {res['links']} 個　"
                      f"內文 {res['body_len']} 字")
                print(f"  {'':22} 樣本：{res['sample']}")

    missing = sorted(set(targets) - {r["vendor"] for r in results})
    print(f"\n[probe] 可爬 {len(results)} 家，沒抓到 {len(missing)} 家")
    if missing:
        print("  沒抓到：" + "、".join(missing))

    usable = [r for r in results if r["body_len"] >= 400]
    print(f"\n內文抓得夠完整（400 字以上）的 {len(usable)} 家，設定如下：\n")
    for r in usable:
        vid = re.sub(r"[^a-z0-9]+", "_", r["vendor"].lower()).strip("_")
        print(f"""  - id: vendor_{vid}
    name: "{r['vendor']}（官方）"
    type: scrape
    tier: vertical
    list_url: "{r['list_url']}"
    link_pattern: '{r['pattern']}'
    base_url: "{r['base_url']}"
    weight: 0.8""")


if __name__ == "__main__":
    main()
