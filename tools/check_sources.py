"""逐一測試每個來源抓不抓得到（2026-10-05 加）。

為什麼需要這支：這天改抓取時弄壞了兩件事，而兩件都是「測錯層」造成的。

1. 傳 render 參數時插錯位置，變成 `source.get("content_selector", render=...)`，
   字典的 get 不收關鍵字參數，所有爬取來源都在這行炸掉。但我驗證時是直接
   呼叫 fetch_scraped_items，繞過了 ingest 的分派層，所以沒測到。
2. 為了救 Endpoints 把 feed 的 UA 換成瀏覽器字串，Fierce Biotech 與
   Illumina 投資人新聞反而變成 403。我只驗證了要修的那一家。

所以這支刻意走 scripts/ingest_topics.py 的 _fetch_source_items，也就是排程
實際會走的那條路，並且一次測全部來源。改完抓取相關的任何東西都該跑它。

不寫資料庫，只抓少量樣本。

用法：
    python -m tools.check_sources
    python -m tools.check_sources --only vendor_tempus_ai,gbimonthly
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingest_topics import _fetch_source_items

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default=None, help="只測這幾個來源 id，逗號分隔")
    parser.add_argument("--days", type=int, default=14, help="往回幾天（預設 14，只要確認抓得到）")
    parser.add_argument("--max-items", type=int, default=3)
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    sources = config["sources"]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        sources = [s for s in sources if s["id"] in wanted]
    fetch_cfg = {"days_back": args.days, "max_items_per_source": args.max_items}

    ok = empty = broken = 0
    print(f"[check_sources] 測 {len(sources)} 個來源（往回 {args.days} 天，各取 {args.max_items} 篇）\n")
    for source in sources:
        label = f"{source['id']} ({source.get('type', 'rss')})"
        try:
            items = _fetch_source_items(source, fetch_cfg)
        except Exception as exc:  # noqa: BLE001 -- 要的就是把每個來源的失敗印出來
            broken += 1
            print(f"  壞掉 {label:40} {type(exc).__name__}: {str(exc)[:60]}")
            continue
        if not items:
            empty += 1
            print(f"  無新文 {label:40}")
            continue
        ok += 1
        lens = [len(i.summary or "") for i in items]
        print(f"  正常 {label:40} {len(items)} 篇 內文 {min(lens)}-{max(lens)} 字")

    print(f"\n正常 {ok}　近期無新文 {empty}　壞掉 {broken}")
    if broken:
        print("有來源壞掉，排程會靜默少抓，先修再出刊。")
        sys.exit(1)


if __name__ == "__main__":
    main()
