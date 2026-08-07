"""一次性批次：對「已標籤但還沒建圖」的積壓文章跑三元組抽取，寫進正式
開發用的 Neo4j（bolt://localhost:7687，不是 neo4j-test）。

跟 scripts/ingest_topics.py 的建圖步驟差別是這支不看本週窗口，把所有
還沒建圖的舊資料一次補完，補完之後常態的每日/每週執行交給
ingest_topics.py 接手（那支只處理本週新進的文章，避免舊積壓佔用當天
額度）。

用法：
    python -m scripts.backfill_graph_extraction
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

import os

from pipeline import graph_builder, graph_store
from pipeline.entity_resolution import EntityResolver
from pipeline.topic_db import get_connection, mark_article_graphed
from pipeline.triple_extraction import extract_triples
from scripts.ingest_topics import _row_to_raw_item

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    conn = get_connection(config["database"]["path"])

    neo4j_uri = os.environ.get("NEO4J_URI")
    if not neo4j_uri:
        print("[backfill_graph_extraction] NEO4J_URI 未設定，中止。")
        return

    driver = graph_store.get_driver(
        neo4j_uri, os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")
    )
    graph_store.ensure_constraints(driver)
    resolver = EntityResolver(graph_store.get_all_canonical_entity_names(driver))

    to_graph = conn.execute(
        """SELECT * FROM articles
           WHERE content_mode IS NOT NULL AND graph_extracted_at IS NULL AND discarded_at IS NULL
           ORDER BY id"""
    ).fetchall()
    total = len(to_graph)
    print(f"[backfill_graph_extraction] 待建圖文章：{total} 篇")

    graphed_count = failed_count = 0
    start = time.time()
    for i, row in enumerate(to_graph, start=1):
        t0 = time.time()
        try:
            item = _row_to_raw_item(row)
            parsed = extract_triples(item)
            stats = graph_builder.add_article_to_graph(driver, resolver, row, parsed["triples"])
            mark_article_graphed(conn, row["id"])
            graphed_count += 1
            elapsed = time.time() - t0
            print(
                f"[backfill_graph_extraction] ({i}/{total}) 文章 {row['id']} 完成，"
                f"{elapsed:.1f}s，{stats}"
            )
        except Exception as exc:  # noqa: BLE001 -- 單篇失敗不中斷整批，下次重跑會重試
            failed_count += 1
            print(f"[backfill_graph_extraction] ({i}/{total}) 文章 {row['id']} 失敗，跳過：{exc}")

    total_elapsed = time.time() - start
    print(
        f"[backfill_graph_extraction] 完成：成功 {graphed_count} 篇，失敗 {failed_count} 篇，"
        f"總耗時 {total_elapsed / 60:.1f} 分鐘。"
    )


if __name__ == "__main__":
    main()
