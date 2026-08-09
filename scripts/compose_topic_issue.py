"""話題式週報：對話題池下一次查詢，組成一期（第三條 pipeline）。

跟抓取/聚類/打分（scripts/ingest_topics.py）完全脫鉤：這支只處理「已經
打過18模組分數、還沒被選用過」的話題，不會自己去抓新資料。

用法：
    python -m scripts.compose_topic_issue                              # weekly，不限日期，issue_date=今天
    python -m scripts.compose_topic_issue --date 2026-08-01 --cadence daily
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from generation.topic_generate import OPENING_TECHNIQUES, generate_topic_article
from pipeline.retrieval import retrieve_sources_for_topic
from pipeline.topic_db import (
    create_issue,
    get_connection,
    mark_topics_published,
    save_generated_topic,
)
from pipeline.topic_selection import select_for_issue
from review.topic_selfcheck import self_check

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def generate_and_check(conn, selected: list[dict], config: dict) -> list[dict]:
    newsletter_name = config["newsletter"]["name"]
    confidence_threshold = config["quality"]["confidence_threshold"]
    max_retries = config["quality"]["max_regeneration_retries"]

    # 開頭手法洗牌後輪流指派給每個入選話題，同一期裡儘量不重複（見
    # generation/topic_generate.py 的 OPENING_TECHNIQUES 說明）。
    shuffled_techniques = OPENING_TECHNIQUES.copy()
    random.shuffle(shuffled_techniques)

    results = []
    for i, entry in enumerate(selected):
        topic_row = entry["row"]
        content_type = entry["content_type"]
        opening_technique = shuffled_techniques[i % len(shuffled_techniques)]
        print(f"[compose_topic_issue] 檢索素材：{topic_row['representative_title'][:50]} ...")

        try:
            source_rows = retrieve_sources_for_topic(conn, config, topic_row)
            article = generate_topic_article(
                newsletter_name, topic_row["representative_title"], content_type, source_rows,
                opening_technique=opening_technique,
            )

            revision_instructions = None
            check_result = None
            for attempt in range(max_retries + 1):
                check_result = self_check(article, source_rows)
                if check_result["confidence"] >= confidence_threshold:
                    break
                revision_instructions = check_result.get("revision_instructions", "")
                if attempt < max_retries:
                    print(
                        f"[compose_topic_issue]   自檢信心度 {check_result['confidence']:.2f} "
                        f"< {confidence_threshold}，回灌重新生成（第 {attempt + 1} 次重試）"
                    )
                    article = generate_topic_article(
                        newsletter_name, topic_row["representative_title"], content_type, source_rows,
                        revision_instructions,
                        opening_technique=opening_technique,
                    )
        except Exception as exc:  # noqa: BLE001 -- 單篇失敗不中斷整批，這個話題留在池裡下次重跑會重新入選
            print(f"[compose_topic_issue]   這篇生成失敗，跳過（下次重跑會重新入選）：{exc}")
            continue

        needs_review = check_result["confidence"] < confidence_threshold
        results.append(
            {
                "topic_id": topic_row["id"],
                "row": topic_row,
                "article": article,
                "source_article_ids": [row["id"] for row in source_rows],
                "confidence": check_result["confidence"],
                "needs_review": needs_review,
            }
        )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date", default=date.today().isoformat(),
        help="這期涵蓋的日期（ISO date）。--cadence daily 時同時當作 issue_date/period_start/period_end；"
        "--cadence weekly 時只影響 issue_date，不限制候選池日期範圍。預設今天。",
    )
    parser.add_argument(
        "--cadence", default="daily", choices=["daily", "weekly"],
        help="出刊頻率，決定用 config/topics.yaml 的 selection.daily 還是 selection.weekly 配額。預設 daily。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    conn = get_connection(config["database"]["path"])

    date_range = (args.date, args.date) if args.cadence == "daily" else None
    selected = select_for_issue(conn, config, cadence=args.cadence, date_range=date_range)
    print(f"[compose_topic_issue] 入選話題：{len(selected)} 個")
    if not selected:
        print("[compose_topic_issue] 話題池裡沒有可用話題（或都已經被選用過），中止。")
        return

    results = generate_and_check(conn, selected, config)
    if not results:
        print("[compose_topic_issue] 入選話題全部生成失敗，沒有組成新的一期，中止。")
        return

    period_start, period_end = date_range if date_range else (None, None)
    issue_id = create_issue(conn, args.date, period_start=period_start, period_end=period_end, cadence=args.cadence)
    for r in results:
        save_generated_topic(
            conn, issue_id, r["topic_id"], r["article"], r["source_article_ids"],
            r["confidence"], r["needs_review"],
        )
    mark_topics_published(conn, [r["topic_id"] for r in results], issue_id)

    pending = sum(1 for r in results if r["needs_review"])
    print(f"[compose_topic_issue] 第 {issue_id} 期已組成，{pending} 個待人工確認。")
    print("[compose_topic_issue] 用 python -m scripts.serve_topics 啟動網頁查看。")


if __name__ == "__main__":
    main()
