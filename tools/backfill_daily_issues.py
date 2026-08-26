"""一次性批次：把 2026-08-01 到 2026-08-09 已抓取但還沒打分的話題按天補打
18 模組分數，再逐天組成日報（cadence="daily"）。

跟 scripts/compose_topic_issue.py 的差別是這支一次跑完整段日期範圍，且
先幫每一天做 pipeline/module_scoring.py 的打分（compose_topic_issue.py
只處理「已經打過分」的話題，不會自己打分）。日期一定要照時間順序處理：
話題選過就標記 published_issue_id、不會被下一天重選，跳著跑或倒著跑會
讓候選池跟預期不一致。

用法：
    python -m tools.backfill_daily_issues
    python -m tools.backfill_daily_issues --start 2026-08-01 --end 2026-08-09
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from pipeline.module_scoring import score_topic
from pipeline.topic_db import (
    create_issue,
    get_articles_for_topic,
    get_connection,
    get_unscored_topics,
    mark_topics_published,
    record_selection_trace,
    save_generated_topic,
    save_module_scores,
)
from pipeline.translate import pretranslate_issue
from scripts.compose_topic_issue import (
    _selected_trace_entries,
    load_config,
    select_and_generate,
)

DEFAULT_START = "2026-08-01"
DEFAULT_END = "2026-08-09"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START, help="回填起始日期（ISO date，含）")
    parser.add_argument("--end", default=DEFAULT_END, help="回填結束日期（ISO date，含）")
    return parser.parse_args()


def _date_range(start: str, end: str) -> list[str]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    days = []
    d = d0
    while d <= d1:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def score_day(conn, config: dict, day: str) -> tuple[int, int]:
    to_score = get_unscored_topics(conn, date_range=(day, day))
    scored_count = failed_count = 0
    for topic_row in to_score:
        try:
            articles = get_articles_for_topic(conn, topic_row["id"])
            parsed = score_topic(topic_row, articles, config["modules"])
            save_module_scores(conn, topic_row["id"], parsed["module_scores"], parsed["content_type"])
            scored_count += 1
        except Exception as exc:  # noqa: BLE001 -- 單篇失敗不中斷整批，下次重跑會重試
            failed_count += 1
            print(f"[backfill_daily_issues]   {day} 打分失敗，跳過：{exc}")
    return scored_count, failed_count


def compose_day(conn, config: dict, day: str) -> dict | None:
    # 冪等防護：這一天已經有日報就跳過，重跑重疊範圍不會出重複的期。
    existing = conn.execute(
        "SELECT id FROM issues WHERE issue_date = ? AND cadence = 'daily'", (day,)
    ).fetchone()
    if existing:
        print(f"[backfill_daily_issues]   {day} 已經有第 {existing['id']} 期，跳過。")
        return {"day": day, "topics": 0, "needs_review": 0, "avg_confidence": None}

    # 候選窗口跟正常日報一致（往回看 carry_over_days 天），不是只看單日。
    # 2026-08-26 修：這裡原本寫死單日窗口，config 放寬 carry_over 後補刊
    # 完全沒吃到，稀疏的日子照樣湊不滿版位。
    carry_days = config["selection"]["daily"].get("carry_over_days", 0)
    range_start = (date.fromisoformat(day) - timedelta(days=carry_days)).isoformat()
    date_range = (range_start, day)
    # 選題＋生成＋失敗補位走跟正常出刊同一條路徑；落選紀錄照樣入帳，回填
    # 出來的期數在網頁上才有選題帳這一頁（見 pipeline/gates.py）。
    results, rejections = select_and_generate(conn, config, "daily", date_range)
    if not results:
        record_selection_trace(conn, issue_date=day, cadence="daily", entries=rejections)
        return {"day": day, "topics": 0, "needs_review": 0, "avg_confidence": None}

    issue_id = create_issue(conn, day, period_start=day, period_end=day, cadence="daily")
    for r in results:
        save_generated_topic(
            conn, issue_id, r["topic_id"], r["article"], r["source_article_ids"],
            r["confidence"], r["needs_review"],
            translations_json=r.get("translations_json"),
        )
    mark_topics_published(conn, [r["topic_id"] for r in results], issue_id)
    record_selection_trace(
        conn,
        issue_date=day,
        cadence="daily",
        entries=_selected_trace_entries(results) + rejections,
        issue_id=issue_id,
    )

    ok, failed = pretranslate_issue(conn, issue_id)
    print(f"[backfill_daily_issues]   英文版預先翻譯：成功 {ok} 篇，失敗 {failed} 篇")

    return {
        "day": day,
        "issue_id": issue_id,
        "topics": len(results),
        "needs_review": sum(1 for r in results if r["needs_review"]),
        "avg_confidence": sum(r["confidence"] for r in results) / len(results),
    }


def main() -> None:
    args = parse_args()
    config = load_config()
    conn = get_connection(config["database"]["path"])

    days = _date_range(args.start, args.end)
    print(f"[backfill_daily_issues] 回填範圍：{args.start} ~ {args.end}（{len(days)} 天）")

    summaries = []
    for day in days:
        print(f"[backfill_daily_issues] === {day} ===")
        scored, failed = score_day(conn, config, day)
        print(f"[backfill_daily_issues]   打分：成功 {scored} 個，失敗 {failed} 個")

        summary = compose_day(conn, config, day)
        if summary is None or summary["topics"] == 0:
            print(f"[backfill_daily_issues]   {day} 沒有可用話題，跳過（不開空期）。")
        else:
            print(
                f"[backfill_daily_issues]   第 {summary['issue_id']} 期已組成："
                f"{summary['topics']} 篇，{summary['needs_review']} 篇待人工確認，"
                f"平均信心度 {summary['avg_confidence']:.2f}"
            )
        summaries.append(summary)

    print("\n[backfill_daily_issues] === 總覽 ===")
    for s in summaries:
        if s["topics"] == 0:
            print(f"  {s['day']}：0 篇")
        else:
            print(
                f"  {s['day']}：{s['topics']} 篇，待確認 {s['needs_review']} 篇，"
                f"平均信心度 {s['avg_confidence']:.2f}"
            )
    print("[backfill_daily_issues] 用 python -m scripts.serve_topics 啟動網頁查看。")


if __name__ == "__main__":
    main()
