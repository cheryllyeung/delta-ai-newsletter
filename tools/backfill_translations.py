"""一次性批次：把還沒有英文版的既有期數補翻。

2026-08-13 之後出刊時會自動預翻（見 scripts/compose_topic_issue.py），這支
是給那之前產出的舊期數用的，補完就不太會再需要。

用法：
    python -m tools.backfill_translations
    python -m tools.backfill_translations --issues 1 2 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from pipeline.topic_db import get_connection
from pipeline.translate import pretranslate_issue

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--issues", type=int, nargs="*", default=None,
        help="只補這幾期（期數 id）。沒帶就補所有還有缺英文版的期數。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    conn = get_connection(config["database"]["path"])

    if args.issues:
        issue_ids = args.issues
    else:
        issue_ids = [
            row["issue_id"]
            for row in conn.execute(
                """SELECT DISTINCT issue_id FROM generated_topics
                   WHERE translations_json IS NULL ORDER BY issue_id"""
            )
        ]

    if not issue_ids:
        print("[backfill_translations] 沒有需要補的期數。")
        return

    print(f"[backfill_translations] 要補的期數：{issue_ids}")
    total_ok = total_failed = 0
    for issue_id in issue_ids:
        ok, failed = pretranslate_issue(conn, issue_id)
        total_ok += ok
        total_failed += failed
        print(f"[backfill_translations] 第 {issue_id} 期：成功 {ok} 篇，失敗 {failed} 篇")

    print(f"[backfill_translations] 完成：成功 {total_ok} 篇，失敗 {total_failed} 篇。")


if __name__ == "__main__":
    main()
