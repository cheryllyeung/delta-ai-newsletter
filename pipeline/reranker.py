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


def rerank(query: str, documents: list[str], top_k: int, min_score: float = 0.0) -> list[int]:
    """回傳 documents 依跟 query 相關度由高到低排序後的原始 index 清單，
    只取前 top_k 個，且分數低於 min_score 的一律不回傳。

    min_score 是必要的：只取前 top_k 名會「湊滿名額」，話題底下只有一篇真正
    相關的文章時，第二個名額還是會被填上，填什麼都好。2026-08-11 實測就出現
    過一篇講 Docker Sandboxes 的文章被塞進一個完全不相干的 GitHub repo 當
    素材，然後寫作真的引用了那個 repo 的 star 數當數據。

    分數是 sigmoid 機率（0~1），實測分布是雙峰的：真正相關的落在 0.8~1.0，
    其餘直接掉到 0.16 以下，中間幾乎是空的，所以門檻很好訂。
    """
    if not documents:
        return []
    model = _get_model()
    scores = model.predict([(query, doc) for doc in documents])
    ranked = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
    kept = [i for i in ranked[:top_k] if scores[i] >= min_score]
    # 保底：全部都低於門檻時至少留最相關的那一篇，不要讓寫作完全沒有素材。
    return kept or ranked[:1]
