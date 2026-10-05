"""探測名單廠商有沒有可用的官方 RSS（2026-10-05 加）。

為什麼需要這支：26 家關注廠商裡有 9 家從來沒出現過，原因不是沒有新聞，是
我們只接媒體、沒接廠商自己的管道。實測也看到 Abbott 推出 Freenome 大腸癌
檢測這種實質動態，唯一報導它的媒體只給標題、抓不到正文。

憑印象猜 feed 網址不可靠，所以這支逐一去試常見位置，只回報真的拿得到
內容的那些，並且印出最新一篇的標題與日期，讓人判斷值不值得收。

這支只讀網路、不碰資料庫，可以隨時跑。

用法：
    python -m tools.probe_vendor_feeds
    python -m tools.probe_vendor_feeds --only Illumina,Natera
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}

# 廠商對應的網域。只有域名是查得到的事實，feed 路徑交給下面的候選清單去試。
# 查不到或不確定的留 None，這支就跳過，不要用猜的網址製造假結果。
VENDOR_DOMAINS: dict[str, str | None] = {
    "Illumina": "illumina.com",
    "Guardant Health": "guardanthealth.com",
    "Foundation Medicine": "foundationmedicine.com",
    "Thermo Fisher": "thermofisher.com",
    "Natera": "natera.com",
    "Freenome": "freenome.com",
    "Tempus AI": "tempus.com",
    "Grail": "grail.com",
    "BillionToOne": "billiontoone.com",
    "PacBio": "pacb.com",
    "Element Biosciences": "elementbiosciences.com",
    "Quest Diagnostics": "questdiagnostics.com",
    "LabCorp": "labcorp.com",
    "Myriad Genetics": "myriad.com",
    "Caris Life Sciences": "carislifesciences.com",
    "Qiagen": "qiagen.com",
    "Centogene": "centogene.com",
    "Sophia Genetics": "sophiagenetics.com",
    "Oxford Nanopore": "nanoporetech.com",
    "華大基因": "bgi.com",
    "Gene Solutions": "genesolutions.com",
    "Macrogen": "macrogen.com",
    "基龍米克斯": "genomics.com.tw",
    "慧智基因": "sofivagenomics.com",
    "訊聯基因": "bionetcorp.com",
    "金萬林": None,
}

# 常見的 feed 位置，照命中率排序。
# 2026-10-05 補上帶 post_type 的變體：Sophia Genetics 的預設 /feed/ 停在
# 2023 年，但 /feed/?post_type=news 是當月的。網站改版後新聞換成另一種
# 文章類型很常見，只試標準路徑會誤判成「這家沒在發消息」。
PATHS = [
    "/rss/news-releases.xml",
    "/feed/",
    "/feed",
    "/feed/?post_type=news",
    "/feed/?post_type=press_release",
    "/feed/?post_type=press-release",
    "/rss",
    "/rss.xml",
    "/news/feed/",
    "/news/rss",
    "/news-room/feed/",
    "/newsroom/feed/",
    "/press/feed/",
    "/press-releases/feed/",
    "/media/feed/",
    "/blog/feed/",
    "/press-releases/rss",
    "/investors/rss",
    "/investor-relations/rss",
]
HOSTS = ["investor.{d}", "ir.{d}", "www.{d}", "{d}"]


def _looks_like_feed(text: str) -> bool:
    head = text[:4000].lower()
    return ("<rss" in head or "<feed" in head) and ("<item" in text.lower() or "<entry" in text.lower())


def _first_item(text: str) -> tuple[str, str]:
    title = re.search(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S | re.I)
    date = re.search(r"<(?:pubDate|updated|published)>(.*?)</(?:pubDate|updated|published)>", text, re.I)
    clean = lambda s: re.sub(r"\s+", " ", s).strip() if s else ""  # noqa: E731
    return clean(title.group(1) if title else ""), clean(date.group(1) if date else "")


_DECLARED = re.compile(
    r"""<link[^>]+type=["']application/(?:rss|atom)\+xml["'][^>]*href=["']([^"']+)["']""", re.I
)


def _declared_feeds(domain: str) -> list[str]:
    """網站自己在首頁宣告的 feed。比盲試路徑準，但宣告的常是預設那份，
    所以兩種都要試（2026-10-05：Sophia 宣告的就是停更的那一份）。"""
    out = []
    for url in (f"https://www.{domain}/", f"https://{domain}/"):
        try:
            r = requests.get(url, timeout=10, headers=UA)
        except Exception:  # noqa: BLE001
            continue
        if r.status_code == 200:
            for href in _DECLARED.findall(r.text):
                if "comment" not in href.lower():
                    out.append(href if href.startswith("http") else f"https://{domain}{href}")
            break
    return out


def _newest(text: str) -> str:
    dates = re.findall(r"<(?:pubDate|updated|published)>(.*?)</(?:pubDate|updated|published)>", text, re.I)
    return dates[0].strip()[:32] if dates else ""


def probe(vendor: str, domain: str) -> tuple[str, str, str, str] | None:
    best: tuple[str, str, str, str] | None = None
    best_year = ""
    for url in _declared_feeds(domain):
        try:
            r = requests.get(url, timeout=10, headers=UA)
        except Exception:  # noqa: BLE001
            continue
        if r.status_code == 200 and _looks_like_feed(r.text):
            title, date = _first_item(r.text)
            year = re.search(r"20\d\d", _newest(r.text))
            if year and year.group(0) > best_year:
                best, best_year = (vendor, url, title[:70], date[:32]), year.group(0)
    for host_tpl in HOSTS:
        host = host_tpl.format(d=domain)
        for path in PATHS:
            url = f"https://{host}{path}"
            try:
                r = requests.get(url, timeout=8, headers=UA, allow_redirects=True)
            except Exception:  # noqa: BLE001 -- 探測失敗就換下一個，不需要分類錯誤
                continue
            if r.status_code != 200 or not _looks_like_feed(r.text):
                continue
            title, date = _first_item(r.text)
            year = re.search(r"20\d\d", _newest(r.text))
            if year and year.group(0) > best_year:
                best, best_year = (vendor, url, title[:70], date[:32]), year.group(0)
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default=None, help="只測這幾家，逗號分隔")
    args = parser.parse_args()

    targets = {k: v for k, v in VENDOR_DOMAINS.items() if v}
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        targets = {k: v for k, v in targets.items() if k in wanted}
    skipped = [k for k, v in VENDOR_DOMAINS.items() if not v]

    print(f"[probe] 測 {len(targets)} 家，跳過 {len(skipped)} 家（網域不確定：{'、'.join(skipped)}）\n")
    found: list[tuple[str, str, str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(lambda kv: probe(*kv), targets.items()):
            if result:
                found.append(result)
                vendor, url, title, date = result
                print(f"  有 feed  {vendor:22} {url}")
                print(f"           最新：{title}　（{date}）")

    missing = sorted(set(targets) - {f[0] for f in found})
    print(f"\n[probe] 找到 {len(found)} 家，沒找到 {len(missing)} 家")
    if missing:
        print("  沒找到：" + "、".join(missing))
    print("\n要納入的話，把找到的網址加到 config/topics.yaml 的 sources，"
          "tier 建議用 vertical（廠商官方管道），weight 0.8。")


if __name__ == "__main__":
    main()
