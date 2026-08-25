"""補跑 Gate 1a：對池裡已經存在的文章做一次收錄判定。

兩種情況要用：
1. 2026-08-14 以前入池的文章沒有 gate_status 欄位，全部是 NULL（讀的時候
   當作 included，見 pipeline/gates.py）
2. 調整了 config/topics.yaml 的 gates.article 門檻，想看新門檻套用到整池
   會是什麼結果

只做 Gate 1a（內文長度、發布日期），不做 Gate 1b（跟 AI 有沒有關係），
因為後者要有標籤結果，而標籤是 LLM 呼叫，補跑整池不是這支的職責。

用法：
    python -m tools.backfill_article_gates --dry-run   # 只看會變成什麼，不寫入
    python -m tools.backfill_article_gates             # 只補還沒判過的（gate_status IS NULL）
    python -m tools.backfill_article_gates --all       # 整池重判，用於改了門檻之後
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import gates
from pipeline.topic_db import get_connection, get_ungated_articles, save_article_gate

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只印出結果，不寫進資料庫")
    parser.add_argument(
        "--all",
        action="store_true",
        help="整池重判（預設只判 gate_status IS NULL 的）。改了門檻之後用這個。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    conn = get_connection(config["database"]["path"])

    if args.all:
        rows = conn.execute("SELECT * FROM articles").fetchall()
        print(f"[backfill_article_gates] 整池重判：{len(rows)} 篇")
    else:
        rows = get_ungated_articles(conn)
        print(f"[backfill_article_gates] 還沒判過的文章：{len(rows)} 篇")

    if not rows:
        print("[backfill_article_gates] 沒有要處理的文章。")
        return

    status_counts: Counter[str] = Counter()
    by_source: dict[str, Counter] = {}
    changed = 0
    for row in rows:
        result = gates.check_article_intake(
            content=row["content"],
            published_at=datetime.fromisoformat(row["published_at"]),
            config=config,
            # 判定基準時間用文章的抓取時間，不是「現在」。用現在的話，這支
            # 每晚一天跑，就會多把一批文章判成「超出窗口」，同一篇文章的
            # 判定結果會隨著執行時間漂移，那樣的帳沒有意義。
            now=datetime.fromisoformat(row["fetched_at"]).astimezone(),
        )
        status_counts[result.status] += 1
        by_source.setdefault(row["source_name"], Counter())[result.status] += 1
        if (row["gate_status"] or None) != result.status:
            changed += 1
        if not args.dry_run:
            save_article_gate(conn, row["id"], result)

    print()
    print(f"  可寫作（included）    {status_counts['included']:>5} 篇")
    print(f"  熱度訊號（signal_only）{status_counts['signal_only']:>5} 篇")
    print(f"  排除（excluded）      {status_counts['excluded']:>5} 篇")
    print(f"  跟原本的判定不同：{changed} 篇")
    print()
    print("依來源：")
    print(f"  {'來源':<36} {'總數':>5} {'可寫作':>7} {'訊號':>6} {'排除':>6}")
    for source_name, counts in sorted(by_source.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counts.values())
        print(
            f"  {source_name[:34]:<36} {total:>5} {counts['included']:>7} "
            f"{counts['signal_only']:>6} {counts['excluded']:>6}"
        )

    if args.dry_run:
        print()
        print("[backfill_article_gates] --dry-run：以上結果沒有寫進資料庫。")
    else:
        print()
        print("[backfill_article_gates] 已寫入。")


if __name__ == "__main__":
    main()
