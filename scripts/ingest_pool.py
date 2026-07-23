"""Delta Pulse v2：抓取候選來源、寫入長期文章池，評分但不淘汰。

可重複執行：同網址已經在 pool 裡的文章不會重複插入。抓取（插入）跟評分是
兩個獨立步驟：每次執行都會對「目前 pool 裡所有還沒評分的文章」評分，
不只是這次新抓到的——這樣如果上次評分跑到一半失敗（例如 API key 沒設好、
連線斷掉），這次重跑會自動把上次留下的爛尾補完，不需要手動處理。

單篇文章評分失敗不會讓整個流程中斷：印出錯誤訊息、跳過這篇，下次重跑
會再試一次（因為它還是「未評分」狀態）。

評分完全部留在 pool 裡，`is_ai_application=false` 只會讓 base_score 打折，
不會讓文章消失，見 pipeline/case_scoring.py 的 compute_base_score()。

用法：
    python -m scripts.ingest_pool
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from ingestion.base import RawItem
from ingestion.case_source import fetch_case_study_items
from pipeline.case_scoring import compute_base_score, score_article
from pipeline.pool_db import get_connection, get_unscored_articles, insert_article_if_new, save_score

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pulse.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _row_to_raw_item(row) -> RawItem:
    """把 DB 裡的一列文章還原成 RawItem，供 score_article() 使用。
    這樣「重新評分之前失敗留下的未評分文章」不用重新打 RSS。
    """
    return RawItem(
        title=row["title"],
        url=row["url"],
        source="case_study",
        subdomain_id=row["source_id"],
        published_at=datetime.fromisoformat(row["published_at"]),
        summary=row["content"],
        score=row["source_weight"],
        extra={"source_name": row["source_name"], "source_weight": row["source_weight"]},
    )


def main() -> None:
    config = load_config()
    conn = get_connection(config["database"]["path"])
    fetch_cfg = config["fetch"]
    selection_cfg = config["selection"]

    inserted_count = 0
    for source in config["sources"]:
        print(f"[ingest_pool] 抓取來源：{source['name']} ...")
        items = fetch_case_study_items(
            source_id=source["id"],
            source_name=source["name"],
            url=source["url"],
            weight=source["weight"],
            days_back=fetch_cfg["days_back"],
            max_items=fetch_cfg["max_items_per_source"],
        )
        for item in items:
            if insert_article_if_new(conn, item) is not None:
                inserted_count += 1
    print(f"[ingest_pool] 本次新增 {inserted_count} 篇文章進入文章池（尚未評分）。")

    to_score = get_unscored_articles(conn)
    print(f"[ingest_pool] 待評分文章（含之前中斷留下的）：{len(to_score)} 篇")

    scored_count = 0
    failed_count = 0
    for row in to_score:
        print(f"[ingest_pool]   評分中：{row['title'][:60]} ...")
        try:
            item = _row_to_raw_item(row)
            parsed = score_article(item)
            base_score = compute_base_score(
                scores=parsed["scores"],
                weights=selection_cfg["scoring_weights"],
                source_weight=row["source_weight"],
                is_ai_application=parsed["is_ai_application"],
                non_application_multiplier=selection_cfg["non_application_score_multiplier"],
            )
            save_score(
                conn,
                row["id"],
                area_category=parsed["area_category"],
                is_ai_application=parsed["is_ai_application"],
                scores=parsed["scores"],
                base_score=base_score,
                one_line_summary=parsed["one_line_summary"],
                key_facts=parsed["key_facts"],
                hashtags=parsed["hashtags"],
            )
            scored_count += 1
        except Exception as exc:  # noqa: BLE001 -- 故意攔截所有例外，單篇失敗不中斷整批
            failed_count += 1
            print(f"[ingest_pool]   評分失敗，跳過（下次重跑會自動重試）：{exc}")

    print(f"[ingest_pool] 完成，評分成功 {scored_count} 篇，失敗 {failed_count} 篇。")


if __name__ == "__main__":
    main()
