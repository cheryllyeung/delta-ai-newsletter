"""一次性批次：按日回補 HF Daily Papers 的歷史熱門 arXiv 論文進文章池。

hf_daily_papers 來源 2026-08-28 才加，正常 ingest 只抓得到當下的榜單，
八月前面的熱門論文要靠這支用 API 的 date 參數逐日補。只做「抓取＋入池
＋Gate 1a」，跟 scripts/ingest_topics.py 的步驟一同一條路；聚類、標籤、
打分不在這裡做，補完跑一次 python -m scripts.ingest_topics 就會接手處理
所有還沒處理的文章。

每天只取 upvote 最高的前 N 篇（預設 10）：整月全收會近千篇，後面每篇都要
過標籤＋打分的 LLM 呼叫，量太大；熱門榜本來就是取頭部才有意義。

用法：
    python -m tools.backfill_hf_papers                          # 8/1 到今天
    python -m tools.backfill_hf_papers --start 2026-08-01 --end 2026-08-10
    python -m tools.backfill_hf_papers --per-day 15
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.hf_papers_source import fetch_hf_daily_papers
from pipeline import gates
from pipeline.topic_db import get_connection, insert_article_if_new, save_article_gate

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-08-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--per-day", type=int, default=10, help="每天取 upvote 最高的前幾篇")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    source = next(s for s in config["sources"] if s["type"] == "hf_papers")
    conn = get_connection(config["database"]["path"])

    day = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    total_inserted = 0
    while day <= end:
        try:
            items = fetch_hf_daily_papers(
                source_id=source["id"],
                source_name=source["name"],
                weight=source["weight"],
                limit=50,
                days_back=3650,  # 回補模式不做時間過濾，日期由 date 參數決定
                min_upvotes=source.get("min_upvotes", 0),
                date=day.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 -- 單日失敗不中斷整段回補
            print(f"[backfill_hf_papers] {day} 抓取失敗，跳過：{exc}")
            day += timedelta(days=1)
            continue

        # API 對週末／未來日期會回滾到最近的榜單，同一批會在多個日期重複
        # 出現，靠 insert_article_if_new 的 URL 去重擋住，不會重複入池。
        items.sort(key=lambda it: -it.score)
        inserted = 0
        for item in items[: args.per_day]:
            item.extra.setdefault("tier", source.get("tier", "depth"))
            item.extra.setdefault("engagement_metric", "hf_upvotes")
            article_id = insert_article_if_new(conn, item)
            if article_id is None:
                continue
            gate = gates.check_article_intake(
                content=item.summary, published_at=item.published_at, config=config
            )
            save_article_gate(conn, article_id, gate)
            inserted += 1
        total_inserted += inserted
        print(f"[backfill_hf_papers] {day}：榜上 {len(items)} 篇，新入池 {inserted} 篇")
        day += timedelta(days=1)
        time.sleep(1)  # 對公開 API 客氣一點

    print(f"[backfill_hf_papers] 完成，共新增 {total_inserted} 篇。")
    print("[backfill_hf_papers] 接著跑 python -m scripts.ingest_topics 做聚類、標籤與打分。")


if __name__ == "__main__":
    main()
