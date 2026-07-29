"""話題式週報：對話題池下一次查詢，組成一期（第三條 pipeline）。

跟抓取/聚類/打分（scripts/ingest_topics.py）完全脫鉤：這支只處理「已經
打過18模組分數、還沒被選用過」的話題，不會自己去抓新資料。

用法：
    python -m scripts.compose_topic_issue
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from generation.topic_generate import generate_topic_article
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

    results = []
    for entry in selected:
        topic_row = entry["row"]
        content_type = entry["content_type"]
        print(f"[compose_topic_issue] 檢索素材：{topic_row['representative_title'][:50]} ...")

        try:
            source_rows = retrieve_sources_for_topic(conn, config, topic_row)
            article = generate_topic_article(newsletter_name, topic_row["representative_title"], content_type, source_rows)

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


def main() -> None:
    config = load_config()
    conn = get_connection(config["database"]["path"])

    selected = select_for_issue(conn, config)
    print(f"[compose_topic_issue] 入選話題：{len(selected)} 個")
    if not selected:
        print("[compose_topic_issue] 話題池裡沒有可用話題（或都已經被選用過），中止。")
        return

    results = generate_and_check(conn, selected, config)
    if not results:
        print("[compose_topic_issue] 入選話題全部生成失敗，沒有組成新的一期，中止。")
        return

    issue_id = create_issue(conn, date.today().isoformat())
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
