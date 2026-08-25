"""補跑 Gate 1b：對池裡已收錄的文章重跑標籤，讓 is_ai_related 判定生效。

為什麼需要這支：2026-08-14 修掉標籤 prompt 的 schema 漏欄位 bug 之前，
is_ai_related 一次都沒出現過（2,463 筆呼叫日誌裡是 0 次），關卡讀不到值
一律當通過。修正只對新文章生效，池裡在那之前標的文章要重標一次，
「非 AI 內容不進候選池」這道關才真的有在管事。

注意 tools/backfill_article_gates.py 只做 Gate 1a（長度、日期），跟這支
互補：那支不花 LLM 額度，這支每篇要打一次 gateway。

只處理「已收錄且已標籤」的文章：signal_only 本來就不標籤不打分，
excluded 的不用救。重標會覆蓋舊標籤（新 prompt 的結果比較完整）。
被判非 AI 的文章標籤照存、gate_status 改成 excluded，跟
scripts/ingest_topics.py 的 _save_tags 行為一致。

用法：
    python -m tools.backfill_article_tags --dry-run    # 只列出會重標哪些，不打 LLM
    python -m tools.backfill_article_tags              # 重標全部已收錄文章
    python -m tools.backfill_article_tags --limit 20   # 先試 20 篇
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import gates
from pipeline.article_tagging import tag_article
from pipeline.topic_db import get_connection, save_article_gate, save_article_tags
from scripts.ingest_topics import _row_to_raw_item, _run_llm_concurrently

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只列出對象，不打 LLM 不寫入")
    parser.add_argument("--limit", type=int, default=None, help="只重標前 N 篇")
    parser.add_argument(
        "--start-after-id",
        type=int,
        default=None,
        help="跳過 id 小於等於這個值的文章，供中斷後續跑（處理順序就是 id 順序）",
    )
    parser.add_argument("--concurrency", type=int, default=8, help="併發數，同 ingest 的預設")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    conn = get_connection(config["database"]["path"])

    rows = conn.execute(
        """SELECT * FROM articles
           WHERE COALESCE(gate_status, 'included') = 'included'
             AND content_mode IS NOT NULL
             AND discarded_at IS NULL
           ORDER BY id"""
    ).fetchall()
    print(f"[backfill_article_tags] 已收錄且已標籤的文章：{len(rows)} 篇")
    if args.start_after_id is not None:
        rows = [r for r in rows if r["id"] > args.start_after_id]
        print(f"[backfill_article_tags]   --start-after-id {args.start_after_id}：續跑剩下的 {len(rows)} 篇。")
    if args.limit is not None:
        rows = rows[: args.limit]
        print(f"[backfill_article_tags]   --limit {args.limit}：這次只重標前 {len(rows)} 篇。")

    if args.dry_run:
        for row in rows[:30]:
            print(f"  #{row['id']:>4} [{row['source_name']}] {row['title'][:60]}")
        if len(rows) > 30:
            print(f"  ……共 {len(rows)} 篇")
        print("[backfill_article_tags] --dry-run：沒有打 LLM，沒有寫入。")
        return

    excluded: list[tuple[int, str, str]] = []
    reason_counts: Counter[str] = Counter()

    def _save(row, parsed) -> None:
        gate = gates.check_article_tagged(parsed, config)
        if not gate.passed:
            save_article_gate(conn, row["id"], gate)
            excluded.append((row["id"], row["title"][:60], gate.detail.get("note", "")))
            reason_counts[gate.reason] += 1
        save_article_tags(
            conn,
            row["id"],
            content_mode=parsed["content_mode"],
            is_case_example=parsed["is_case_example"],
            case_industry=parsed.get("case_industry"),
            case_department=parsed.get("case_department"),
            case_outcome=parsed.get("case_outcome"),
            tech_tags=parsed["tech_tags"],
            entity_tags=parsed["entity_tags"],
            scenario_tags=parsed["scenario_tags"],
            industry_tags=parsed["industry_tags"],
            one_line_summary=parsed["one_line_summary"],
        )

    ok, failed = _run_llm_concurrently(
        rows,
        work=lambda row: tag_article(_row_to_raw_item(row)),
        handle=_save,
        describe=lambda row: row["title"][:60],
        concurrency=args.concurrency,
        label="重標",
    )

    print()
    print(f"[backfill_article_tags] 重標完成：成功 {ok} 篇，失敗 {failed} 篇。")
    print(f"  判定非 AI 而排除：{len(excluded)} 篇")
    for article_id, title, note in excluded:
        print(f"    #{article_id:>4} {title}")
        if note:
            print(f"          理由：{note}")
    if failed:
        print("  失敗的下次重跑會自動補（條件是 gate_status 還是 included）。")


if __name__ == "__main__":
    main()
