"""話題式週報的向量庫：Qdrant 本機 embedded 模式（不需要另外跑伺服器/Docker）。

`QdrantClient(path=...)` 是 Qdrant 官方提供的內嵌模式，資料直接寫在本機
檔案系統，符合 PRD「PoC 免費優先、單機部署」的精神，也不用額外裝/管一個
資料庫服務。之後真的要多人共用、跑到公司 VM，再評估要不要換成
Qdrant server 模式（改個 client 建立方式就好，collection/查詢介面不變）。

article 的 Qdrant point id 直接沿用 pipeline/topic_db.py 裡 articles 表的
自增 id，兩邊用同一把 key，不另外維護對應表。
"""
from __future__ import annotations

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

_clients: dict[str, QdrantClient] = {}


def get_client(path: str) -> QdrantClient:
    """同一個 path 只建立一次 client（Qdrant 本機模式同一時間只能有一個
    process 持有某個 path 的 lock，重複建立會報錯）。"""
    if path not in _clients:
        _clients[path] = QdrantClient(path=path)
    return _clients[path]


def ensure_collection(client: QdrantClient, collection: str, vector_size: int) -> None:
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


def upsert_article(
    client: QdrantClient, collection: str, article_id: int, vector: np.ndarray, payload: dict
) -> None:
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=article_id, vector=vector.tolist(), payload=payload)],
    )


def search_similar(
    client: QdrantClient,
    collection: str,
    vector: np.ndarray,
    top_k: int,
    exclude_id: int | None = None,
) -> list[tuple[int, float, dict]]:
    """回傳 [(article_id, cosine_similarity, payload), ...]，依相似度由高到低。
    exclude_id 用來排除文章自己（例如檢索階段拿某篇文章去找池裡其他相關文章時）。
    做法是多撈一筆再過濾掉自己，Qdrant 本機模式的 collection 量體（PoC 規模
    約數千筆）沒有大到需要用 filter 做這件事。
    """
    limit = top_k + 1 if exclude_id is not None else top_k
    result = client.query_points(collection_name=collection, query=vector.tolist(), limit=limit)
    hits = [(pt.id, pt.score, pt.payload or {}) for pt in result.points]
    if exclude_id is not None:
        hits = [h for h in hits if h[0] != exclude_id]
    return hits[:top_k]
