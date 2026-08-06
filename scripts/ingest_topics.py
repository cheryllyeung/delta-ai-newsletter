"""話題式週報：抓取候選來源、聚類成話題、標籤與打分（第三條 pipeline）。

跟 scripts/ingest_pool.py 是平行模組，同樣可重複執行：同網址已在池裡的
文章不會重複插入；聚類、標籤、打分都只處理「還沒做過那一步」的資料，
上次意外中斷（例如 LLM_API_KEY 沒打通）這次重跑會自動接著做，不用手動
清理。

分五步，每一步都獨立、任何一步失敗不影響前面已經完成的部分：
1. 抓取候選來源，插入文章池（依 url 去重）
2. 話題聚類：embedding + Qdrant 找最近鄰居，相似度夠高就併入既有話題
   （這一步跑在本機，不需要 LLM_API_KEY）
3. 標籤抽取：每篇「已聚類但還沒標籤」的文章跑 Prompt（多維關鍵詞、
   content_mode、案例標記）
3.5. 知識圖譜抽取：每篇「已標籤但還沒建圖」的文章跑三元組抽取 Prompt，
   經 EntityResolver 解析實體後寫進 Neo4j（NEO4J_URI 沒設就跳過這一步）
4. 18 模組打分：每個「話題內所有文章都已標籤」的話題跑 Prompt

用法：
    python -m scripts.ingest_topics
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from ingestion.arxiv_source import fetch_arxiv_items
from ingestion.base import RawItem
from ingestion.case_source import fetch_case_study_items
from ingestion.github_source import fetch_github_items
from ingestion.hn_source import fetch_hn_items
from ingestion.reddit_source import fetch_reddit_items
from ingestion.stackexchange_source import fetch_stackexchange_items
from pipeline import graph_builder, graph_store
from pipeline.article_tagging import tag_article
from pipeline.entity_resolution import EntityResolver
from pipeline.module_scoring import score_topic
from pipeline.topic_clustering import cluster_new_articles
from pipeline.topic_db import (
    discard_stale_ungraphed_articles,
    discard_stale_untagged_articles,
    get_articles_for_topic,
    get_connection,
    get_ungraphed_articles,
    get_untagged_articles,
    get_unscored_topics,
    insert_article_if_new,
    mark_article_graphed,
    save_article_tags,
    save_module_scores,
    week_start_date,
)
from pipeline.triple_extraction import extract_triples

# item.source（RawItem 的來源平台字串，見 ingestion/base.py）→ 這個平台的
# 熱門度指標叫什麼名字。只有這幾個訊號層來源真的有「群眾熱度」數字（HN
# points、Reddit upvotes、GitHub stars、StackExchange score），核心層 RSS
# 只有靜態的 source_weight、arXiv 的 score 是關鍵字命中分數（相關性，不是
# 熱度），都不該塞進 engagement_raw，見 pipeline/topic_db.py 的說明。
_ENGAGEMENT_METRIC_BY_SOURCE = {
    "hackernews": "hn_points",
    "reddit": "reddit_score",
    "github": "github_stars",
    "stackexchange": "stackexchange_score",
}

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _row_to_raw_item(row) -> RawItem:
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


def _fetch_source_items(source: dict, fetch_cfg: dict) -> list[RawItem]:
    """依 source["type"] 分派到對應的抓取函式，預設當作 RSS 處理
    （向下相容舊設定沒寫 type 的來源）。任何一個來源抓取失敗都不該
    中斷其他來源，由呼叫端包 try/except。"""
    source_type = source.get("type", "rss")

    if source_type == "rss":
        return fetch_case_study_items(
            source_id=source["id"],
            source_name=source["name"],
            url=source["url"],
            weight=source["weight"],
            days_back=fetch_cfg["days_back"],
            max_items=fetch_cfg["max_items_per_source"],
        )

    if source_type == "arxiv":
        items = fetch_arxiv_items(
            subdomain_id=source["id"],
            categories=source["categories"],
            keywords=source.get("keywords", []),
            max_results=fetch_cfg["max_items_per_source"],
            days_back=fetch_cfg["days_back"],
            timeout=30,
        )
        for item in items:
            item.extra.setdefault("source_name", source["name"])
            item.extra.setdefault("source_weight", source["weight"])
        return items

    if source_type == "hn":
        return fetch_hn_items(
            source_id=source["id"],
            source_name=source["name"],
            queries=source["queries"],
            weight=source["weight"],
            min_points=source.get("min_points", 20),
            days_back=fetch_cfg["days_back"],
            max_items=fetch_cfg["max_items_per_source"],
        )

    if source_type == "reddit":
        items = fetch_reddit_items(
            subdomain_id=source["id"],
            subreddits=source["subreddits"],
            limit_per_subreddit=fetch_cfg["max_items_per_source"],
        )
        for item in items:
            item.extra.setdefault("source_name", source["name"])
            item.extra.setdefault("source_weight", source["weight"])
        return items

    if source_type == "stackexchange":
        return fetch_stackexchange_items(
            source_id=source["id"],
            source_name=source["name"],
            tags=source["tags"],
            weight=source["weight"],
            site=source.get("site", "stackoverflow"),
            min_score=source.get("min_score", 1),
            days_back=fetch_cfg["days_back"],
            max_items=fetch_cfg["max_items_per_source"],
        )

    if source_type == "github":
        return fetch_github_items(
            source_id=source["id"],
            source_name=source["name"],
            queries=source["queries"],
            weight=source["weight"],
            min_stars=source.get("min_stars", 20),
            days_back=fetch_cfg["days_back"],
            max_items=fetch_cfg["max_items_per_source"],
        )

    raise ValueError(f"未知的來源類型：{source_type}（來源：{source['name']}）")


def main() -> None:
    config = load_config()
    conn = get_connection(config["database"]["path"])
    fetch_cfg = config["fetch"]

    # 步驟一：抓取
    inserted_count = 0
    for source in config["sources"]:
        print(f"[ingest_topics] 抓取來源：{source['name']} ...")
        try:
            items = _fetch_source_items(source, fetch_cfg)
        except Exception as exc:  # noqa: BLE001 -- 單一來源失敗不中斷其他來源
            print(f"[ingest_topics]   抓取失敗，跳過這個來源：{exc}")
            continue
        for item in items:
            item.extra.setdefault("tier", source.get("tier", "case"))
            engagement_metric = _ENGAGEMENT_METRIC_BY_SOURCE.get(item.source)
            if engagement_metric:
                item.extra.setdefault("engagement_metric", engagement_metric)
            if insert_article_if_new(conn, item) is not None:
                inserted_count += 1
    print(f"[ingest_topics] 本次新增 {inserted_count} 篇文章進入文章池（尚未聚類）。")

    # 步驟二：話題聚類（本機 embedding + Qdrant，不需要 LLM_API_KEY）
    print("[ingest_topics] 話題聚類中（本機 embedding，第一次執行會下載模型權重）...")
    embed_cfg = config["embedding"]
    vector_cfg = config["vector_store"]
    cluster_stats = cluster_new_articles(
        conn,
        qdrant_path=vector_cfg["path"],
        collection=vector_cfg["collection"],
        similarity_threshold=embed_cfg["cluster_similarity_threshold"],
    )
    print(
        f"[ingest_topics] 聚類完成：處理 {cluster_stats['processed']} 篇，"
        f"新開話題 {cluster_stats['new_topics']} 個，併入既有話題 {cluster_stats['merged']} 篇。"
    )

    # 步驟三：標籤抽取（只處理本週窗口內的文章，窗口外的舊積壓先標記捨棄，
    # 避免霸占當天標籤額度，詳見 pipeline/topic_db.py 的說明）
    week_start = week_start_date()
    discarded_count = discard_stale_untagged_articles(conn, week_start)
    if discarded_count:
        print(f"[ingest_topics] 本週（{week_start}）以前還沒標籤的舊文章，已捨棄 {discarded_count} 篇。")
    to_tag = get_untagged_articles(conn, week_start)
    print(f"[ingest_topics] 待標籤文章：{len(to_tag)} 篇")
    tagged_count = failed_tag_count = 0
    for row in to_tag:
        print(f"[ingest_topics]   標籤中：{row['title'][:60]} ...")
        try:
            item = _row_to_raw_item(row)
            parsed = tag_article(item)
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
            tagged_count += 1
        except Exception as exc:  # noqa: BLE001 -- 單篇失敗不中斷整批，下次重跑會重試
            failed_tag_count += 1
            print(f"[ingest_topics]   標籤失敗，跳過（下次重跑會自動重試）：{exc}")
    print(f"[ingest_topics] 標籤完成：成功 {tagged_count} 篇，失敗 {failed_tag_count} 篇。")

    # 步驟三之五：知識圖譜抽取（三元組 LLM 呼叫 + 實體解析 + 寫入 Neo4j）。
    # NEO4J_URI 沒設就跳過整段，不讓其他步驟中斷（跟 REDDIT_* 沒填時的
    # 降級行為一致）。
    neo4j_uri = os.environ.get("NEO4J_URI")
    if not neo4j_uri:
        print("[ingest_topics] NEO4J_URI 未設定，跳過知識圖譜抽取步驟。")
    else:
        discarded_graph_count = discard_stale_ungraphed_articles(conn, week_start)
        if discarded_graph_count:
            print(f"[ingest_topics] 本週以前還沒建圖的舊文章，已捨棄 {discarded_graph_count} 篇。")
        to_graph = get_ungraphed_articles(conn, week_start)
        print(f"[ingest_topics] 待建圖文章：{len(to_graph)} 篇")

        driver = graph_store.get_driver(
            neo4j_uri, os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")
        )
        graph_store.ensure_constraints(driver)
        resolver = EntityResolver(graph_store.get_all_canonical_entity_names(driver))

        graphed_count = failed_graph_count = 0
        for row in to_graph:
            print(f"[ingest_topics]   建圖中：{row['title'][:60]} ...")
            try:
                item = _row_to_raw_item(row)
                parsed = extract_triples(item)
                graph_builder.add_article_to_graph(driver, resolver, row, parsed["triples"])
                mark_article_graphed(conn, row["id"])
                graphed_count += 1
            except Exception as exc:  # noqa: BLE001 -- 單篇失敗不中斷整批，下次重跑會重試
                failed_graph_count += 1
                print(f"[ingest_topics]   建圖失敗，跳過（下次重跑會自動重試）：{exc}")
        print(f"[ingest_topics] 建圖完成：成功 {graphed_count} 篇，失敗 {failed_graph_count} 篇。")

    # 步驟四：18 模組打分
    to_score = get_unscored_topics(conn)
    print(f"[ingest_topics] 待打分話題：{len(to_score)} 個")
    scored_count = failed_score_count = 0
    for topic_row in to_score:
        print(f"[ingest_topics]   打分中：{topic_row['representative_title'][:60]} ...")
        try:
            articles = get_articles_for_topic(conn, topic_row["id"])
            parsed = score_topic(topic_row, articles, config["modules"])
            save_module_scores(conn, topic_row["id"], parsed["module_scores"], parsed["content_type"])
            scored_count += 1
        except Exception as exc:  # noqa: BLE001
            failed_score_count += 1
            print(f"[ingest_topics]   打分失敗，跳過（下次重跑會自動重試）：{exc}")
    print(f"[ingest_topics] 打分完成：成功 {scored_count} 個，失敗 {failed_score_count} 個。")


if __name__ == "__main__":
    main()
