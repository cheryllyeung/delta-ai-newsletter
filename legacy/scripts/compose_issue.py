"""Delta Pulse v2：對長期文章池下一次查詢，組成一期電子報。

跟抓取/評分（scripts/ingest_pool.py）完全脫鉤：這支只處理「pool 裡已經
評分過、還沒被選用過」的文章，不會自己去抓新資料。導言（Prompt 3）依
這期實際選出的內容動態生成，沒有預設主題可套用。

用法：
    python -m scripts.compose_issue
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from generation.case_generate import generate_case
from generation.case_intro import generate_intro
from pipeline.pool_db import (
    create_issue,
    get_connection,
    mark_published,
    save_generated_case,
)
from pipeline.pool_selection import select_for_issue
from review.case_selfcheck import self_check

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pulse.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def generate_and_check(selected: list[dict], config: dict) -> list[dict]:
    newsletter_name = config["newsletter"]["name"]
    confidence_threshold = config["quality"]["confidence_threshold"]
    max_retries = config["quality"]["max_regeneration_retries"]

    cases = []
    for entry in selected:
        row = entry["row"]
        tags = entry["tags"]
        key_facts = json.loads(row["key_facts_json"])
        print(f"[compose_issue] 生成案例：{row['title'][:50]} ...")

        article = generate_case(newsletter_name, row, tags, key_facts)
        revision_instructions = None
        check_result = None

        for attempt in range(max_retries + 1):
            check_result = self_check(article, key_facts, row["content"])
            if check_result["confidence"] >= confidence_threshold:
                break
            revision_instructions = check_result.get("revision_instructions", "")
            if attempt < max_retries:
                print(
                    f"[compose_issue]   自檢信心度 {check_result['confidence']:.2f} "
                    f"< {confidence_threshold}，回灌重新生成（第 {attempt + 1} 次重試）"
                )
                article = generate_case(newsletter_name, row, tags, key_facts, revision_instructions)

        needs_review = check_result["confidence"] < confidence_threshold
        cases.append(
            {
                "article_id": row["id"],
                "row": row,
                "tags": tags,
                "article": article,
                "confidence": check_result["confidence"],
                "needs_review": needs_review,
            }
        )
    return cases


def main() -> None:
    config = load_config()
    conn = get_connection(config["database"]["path"])

    selected = select_for_issue(conn, config)
    print(f"[compose_issue] 入選案例：{len(selected)} 篇")
    if not selected:
        print("[compose_issue] 文章池裡沒有可用文章（或都已經被選用過），中止。")
        return

    cases = generate_and_check(selected, config)

    tag_counter: Counter[str] = Counter()
    area_counter: Counter[str] = Counter()
    for c in cases:
        tag_counter.update(c["tags"])
        area_counter[c["row"]["area_category"]] += 1
    dominant_tags = [tag for tag, _ in tag_counter.most_common(5)]

    intro = generate_intro(
        newsletter_name=config["newsletter"]["name"],
        selected_cases_summaries=[c["article"]["card_summary"]["text"] for c in cases],
        dominant_tags=dominant_tags,
        area_breakdown=dict(area_counter),
    )

    issue_id = create_issue(conn, date.today().isoformat(), intro["hook"], intro["signal"])
    for c in cases:
        save_generated_case(
            conn, issue_id, c["article_id"], c["article"], c["confidence"], c["needs_review"]
        )
    mark_published(conn, [c["article_id"] for c in cases], issue_id)

    pending = sum(1 for c in cases if c["needs_review"])
    print(f"[compose_issue] 第 {issue_id} 期已組成，{pending} 篇待人工確認。")
    print("[compose_issue] 用 python -m scripts.serve_pulse 啟動網頁查看。")


if __name__ == "__main__":
    main()
