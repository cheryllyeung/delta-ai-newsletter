"""階段四：對入選話題做兩段式檢索，撈出寫作要用的素材（PRD §4 階段四）。

第一段：用話題的代表文字 embedding，從全池（不只是話題自己的文章）撈
top-20 最相似的文章，因為即使話題本身只有 1 篇來源文章（目前來源都是
企業案例，同事件被多家媒體報導、需要合併成同一話題的情況很少見，見
pipeline/topic_clustering.py 的說明），這一步仍然可以從全池撈到其他
主題相關的文章補充脈絡，不會讓寫作素材只剩孤零零一篇。

第二段：reranker 對這 top-20 重排，取分數夠高的前幾篇當最終寫作素材。
篇數上限是 reranker.rerank_top_k，但**不會湊滿**：低於 min_relevance 的候選
一律不給寫作看到，寧可只給一篇也不要摻進不相干的素材（見 pipeline/
reranker.py 的說明）。
"""
from __future__ import annotations

import sqlite3

from pipeline import embeddings, reranker, vector_store
from pipeline.topic_db import get_articles_by_ids, get_articles_for_topic


def _query_text_for_topic(topic_row: sqlite3.Row, topic_articles: list[sqlite3.Row]) -> str:
    summaries = "\n".join(row["one_line_summary"] or "" for row in topic_articles)
    return f"{topic_row['representative_title']}\n{summaries}"


def retrieve_sources_for_topic(
    conn: sqlite3.Connection, config: dict, topic_row: sqlite3.Row
) -> list[sqlite3.Row]:
    """回傳依 reranker 相關度排序（高到低）的來源文章列，供
    generation/topic_generate.py 寫作使用。"""
    vector_cfg = config["vector_store"]
    reranker_cfg = config["reranker"]

    topic_articles = get_articles_for_topic(conn, topic_row["id"])
    query_text = _query_text_for_topic(topic_row, topic_articles)
    query_vector = embeddings.encode_one(query_text)

    client = vector_store.get_client(vector_cfg["path"])
    hits = vector_store.search_similar(
        client, vector_cfg["collection"], query_vector, top_k=reranker_cfg["retrieval_top_k"]
    )
    candidate_ids = [hit[0] for hit in hits]
    if not candidate_ids:
        # 保底：向量庫查不到（理論上不該發生，話題內文章一定進過向量庫）
        # 就退回話題自己的文章，避免整段檢索空手而回。
        candidate_ids = [row["id"] for row in topic_articles]

    rows_by_id = {row["id"]: row for row in get_articles_by_ids(conn, candidate_ids)}
    ordered_candidates = [rows_by_id[cid] for cid in candidate_ids if cid in rows_by_id]

    doc_texts = [f"{row['title']}\n{row['content'][:2000]}" for row in ordered_candidates]
    top_indices = reranker.rerank(
        query_text,
        doc_texts,
        top_k=reranker_cfg["rerank_top_k"],
        min_score=reranker_cfg.get("min_relevance", 0.0),
    )
    return [ordered_candidates[i] for i in top_indices]
