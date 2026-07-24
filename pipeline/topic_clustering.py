"""階段二的核心邏輯：把新文章跟現有文章比對向量相似度，同事件多來源報導
合併成同一個「話題」（依 docs/0724_v3_PRD.md 階段二設計）。

做法（PoC 階段刻意簡化，屬單一連結式的貪婪聚類，不是正式的聚類演算法，
之後要調準度可以在這裡換更嚴謹的方法，不用動呼叫端）：
每篇新文章 embedding 之後，去 Qdrant 找池裡向量最相似的既有文章。如果
相似度超過門檻，就併入那篇文章所屬的話題；否則自己開一個新話題。
這篇文章的向量隨後也會存進 Qdrant，讓後面的文章可以跟它比對。

跟評分/標籤（pipeline/article_tagging.py、pipeline/module_scoring.py）完全
脫鉤：這裡只決定「這篇文章屬於哪個話題」，不管內容好壞。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline import embeddings, vector_store
from pipeline.topic_db import (
    assign_article_to_topic,
    create_topic,
    get_unclustered_articles,
)


def _embedding_text(row: sqlite3.Row) -> str:
    """拿標題+內文前段做 embedding，全文太長會拖慢速度、也稀釋標題訊號，
    新聞類文章的主題通常標題+前幾段就夠代表整篇在講什麼。"""
    return f"{row['title']}\n\n{row['content'][:2000]}"


def cluster_new_articles(
    conn: sqlite3.Connection,
    qdrant_path: str,
    collection: str,
    similarity_threshold: float,
) -> dict:
    """對 pool 裡還沒聚類的文章逐一跑聚類，回傳統計數字方便呼叫端印 log。"""
    rows = get_unclustered_articles(conn)
    if not rows:
        return {"processed": 0, "new_topics": 0, "merged": 0}

    client = vector_store.get_client(qdrant_path)
    vector_store.ensure_collection(client, collection, embeddings.embedding_dimension())

    new_topics = 0
    merged = 0
    for row in rows:
        vector = embeddings.encode_one(_embedding_text(row))
        seen_at = datetime.now(timezone.utc).isoformat()

        hits = vector_store.search_similar(client, collection, vector, top_k=1)
        if hits and hits[0][1] >= similarity_threshold:
            topic_id = hits[0][2]["topic_id"]
            merged += 1
        else:
            topic_id = create_topic(conn, representative_title=row["title"], seen_at=seen_at)
            new_topics += 1

        assign_article_to_topic(conn, row["id"], topic_id, seen_at)
        vector_store.upsert_article(
            client,
            collection,
            article_id=row["id"],
            vector=vector,
            payload={"topic_id": topic_id, "title": row["title"], "url": row["url"]},
        )

    return {"processed": len(rows), "new_topics": new_topics, "merged": merged}
