"""補跑發佈判定：對池裡所有已標籤、還沒判過的文章跑 release_check。

為什麼需要這支：發佈判定是 2026-08-28 加的（pipeline/release_check.py），
在那之前入池的文章都沒有 is_release 標記，網頁的「模型與工具發佈」頁
（/releases）會看不到歷史發佈。ingest 只處理本週窗口內的文章，窗口外的
就靠這支補。

跟 tools/backfill_article_tags.py 同一個骨架，但這支不重標、不動 gate：
只補 is_release 這組欄位，已有標記的（release_checked_at 非 NULL）跳過，
中斷重跑會自動接續。

用法：
    python -m tools.backfill_release_check --dry-run    # 只列出對象，不打 LLM
    python -m tools.backfill_release_check              # 補整個池
    python -m tools.backfill_release_check --limit 20   # 先試 20 篇
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.release_check import check_release
from pipeline.topic_db import (
    get_connection,
    get_release_unchecked_articles,
    save_article_release,
)
from scripts.ingest_topics import _row_to_raw_item, _run_llm_concurrently

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只列出對象，不打 LLM 不寫入")
    parser.add_argument("--limit", type=int, default=None, help="只判前 N 篇")
    parser.add_argument("--concurrency", type=int, default=8, help="併發數，同 ingest 的預設")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    conn = get_connection(config["database"]["path"])

    rows = get_release_unchecked_articles(conn)  # 不帶窗口，補整個池
    print(f"[backfill_release_check] 待發佈判定的文章：{len(rows)} 篇")
    if args.limit is not None:
        rows = rows[: args.limit]
        print(f"[backfill_release_check]   --limit {args.limit}：這次只判前 {len(rows)} 篇。")

    if args.dry_run:
        for row in rows[:30]:
            print(f"  #{row['id']:>4} [{row['source_name']}] {row['title'][:60]}")
        if len(rows) > 30:
            print(f"  ……共 {len(rows)} 篇")
        print("[backfill_release_check] --dry-run：沒有打 LLM，沒有寫入。")
        return

    found: list[tuple[int, str, str]] = []

    def _save(row, parsed) -> None:
        save_article_release(conn, row["id"], parsed)
        if parsed.get("is_release"):
            found.append((row["id"], parsed.get("vendor") or "?", parsed.get("product") or row["title"][:50]))

    ok, failed = _run_llm_concurrently(
        rows,
        work=lambda row: check_release(_row_to_raw_item(row)),
        handle=_save,
        describe=lambda row: row["title"][:60],
        concurrency=args.concurrency,
        label="發佈判定",
    )

    print()
    print(f"[backfill_release_check] 完成：成功 {ok} 篇，失敗 {failed} 篇。")
    print(f"  判定為發佈：{len(found)} 篇")
    for article_id, vendor, product in found:
        print(f"    #{article_id:>4} [{vendor}] {product}")
    if failed:
        print("  失敗的下次重跑會自動補（release_checked_at 還是 NULL）。")


if __name__ == "__main__":
    main()
