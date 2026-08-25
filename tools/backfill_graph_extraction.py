"""一次性批次：對「已標籤但還沒建圖」的積壓文章跑三元組抽取，寫進正式
開發用的 Neo4j（bolt://localhost:7687，不是 neo4j-test）。

跟 scripts/ingest_topics.py 的建圖步驟差別是這支不看本週窗口，把所有
還沒建圖的舊資料一次補完，補完之後常態的每日/每週執行交給
ingest_topics.py 接手（那支只處理本週新進的文章，避免舊積壓佔用當天
額度）。

三元組抽取（LLM 呼叫）平行跑、圖寫入序列跑，理由見 main() 裡的註解。
併發條數用環境變數 BACKFILL_CONCURRENCY 調整，預設 8。

用法：
    python -m tools.backfill_graph_extraction
    BACKFILL_CONCURRENCY=16 python -m tools.backfill_graph_extraction
"""
from __future__ import annotations

import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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

# 同時在途的三元組抽取請求數。實測公司內網 gateway（Qwen3.5-122B-A10B）跑
# 真實 triple_extraction prompt（填滿 6000 字上限）：單篇序列 5.3 秒，8 條
# 併發每篇 1.51 秒，16 條併發每篇 0.90 秒，兩個檔位都零失敗。8 到 16 的加速
# 是次線性的（兩倍 worker 只換到 1.68 倍），代表已經在逼近共用容量上限，
# 而且那只是短爆發測試，沒測連續數百次的持續負載會不會觸發速率限制。預設
# 訂 8 是留餘裕給其他同事共用這個 gateway，要衝再用環境變數調。
_DEFAULT_CONCURRENCY = 8


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

    concurrency = int(os.environ.get("BACKFILL_CONCURRENCY", _DEFAULT_CONCURRENCY))
    print(f"[backfill_graph_extraction] 三元組抽取併發 {concurrency} 條，圖寫入序列")

    # 只有 extract_triples（純 LLM 呼叫、無共用狀態）平行跑。圖寫入這一段
    # 留在主執行緒依序處理，不是保守，是正確性要求：
    #   * EntityResolver 是有狀態且順序相關的——先 resolve A 再 resolve B，
    #     跟反過來可能選出不同的 canonical，平行化會讓結果不可重現
    #   * sqlite connection 預設不能跨執行緒共用
    # 而抽取本來就是整批耗時的絕大部分（實測單篇 5.3 秒，圖寫入是毫秒級），
    # 所以把序列的部分留下來幾乎不影響總時間。
    #
    # 結果依 submit 順序（也就是 SQL 的 ORDER BY id）消費，不用完成順序，
    # 這樣同一批資料重跑會得到同一組 canonical 實體。
    graphed_count = failed_count = 0
    start = time.time()
    rows = iter(to_graph)
    done = 0

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        inflight: deque = deque()

        def submit_next() -> bool:
            row = next(rows, None)
            if row is None:
                return False
            inflight.append((row, pool.submit(extract_triples, _row_to_raw_item(row))))
            return True

        # 多押一輪的量，讓主執行緒在寫圖時 worker 不會閒著。
        for _ in range(concurrency * 2):
            if not submit_next():
                break

        while inflight:
            row, future = inflight.popleft()
            done += 1
            t0 = time.time()
            try:
                parsed = future.result()
                stats = graph_builder.add_article_to_graph(driver, resolver, row, parsed["triples"])
                mark_article_graphed(conn, row["id"])
                graphed_count += 1
                print(
                    f"[backfill_graph_extraction] ({done}/{total}) 文章 {row['id']} 完成，"
                    f"{time.time() - t0:.1f}s，{stats}"
                )
            except Exception as exc:  # noqa: BLE001 -- 單篇失敗不中斷整批，下次重跑會重試
                failed_count += 1
                print(f"[backfill_graph_extraction] ({done}/{total}) 文章 {row['id']} 失敗，跳過：{exc}")
            submit_next()

    total_elapsed = time.time() - start
    print(
        f"[backfill_graph_extraction] 完成：成功 {graphed_count} 篇，失敗 {failed_count} 篇，"
        f"總耗時 {total_elapsed / 60:.1f} 分鐘。"
    )


if __name__ == "__main__":
    main()
