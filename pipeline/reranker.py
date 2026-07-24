"""話題式週報的 reranker：bge-reranker-v2-m3，寫作階段兩段式檢索的第二段
（PRD §6：小 embedding 撈 top-20 + reranker 重排取 top-5，檢索品質通常優於
單用大型 embedding 模型，且資源需求低一個量級）。

用 sentence-transformers 的 CrossEncoder 載入，跟 pipeline/embeddings.py
同樣是本機免費模型，不需要 LLM_API_KEY，一樣是 lazy-singleton 寫法。
"""
from __future__ import annotations

_MODEL_NAME = "BAAI/bge-reranker-v2-m3"

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder

        _model = CrossEncoder(_MODEL_NAME)
    return _model


def rerank(query: str, documents: list[str], top_k: int) -> list[int]:
    """回傳 documents 依跟 query 相關度由高到低排序後的原始 index 清單，
    只取前 top_k 個。"""
    if not documents:
        return []
    model = _get_model()
    scores = model.predict([(query, doc) for doc in documents])
    ranked = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
    return ranked[:top_k]
