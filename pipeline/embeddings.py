"""話題式週報用的全文 embedding：Qwen3-Embedding-0.6B。

跟 legacy/pipeline/tag_clustering.py 的本機 embedding 用途不同：那裡是拿來合併
「同義詞標籤」這種短詞組，這裡是拿來把整篇文章嵌入向量，供
pipeline/vector_store.py 存進 Qdrant 做話題聚類與檢索用（PRD §6 選型理由：
中文語料深度佳、0.6B 在 CPU 可跑、免費開源）。

一樣用 sentence-transformers 的 lazy-singleton 寫法：第一次呼叫才載入模型
（會自動下載權重，約 1.2GB），之後重複使用同一個 model 物件。
"""
from __future__ import annotations

import numpy as np

_MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embedding_dimension() -> int:
    return _get_model().get_embedding_dimension()


def encode(texts: list[str]) -> np.ndarray:
    """回傳 L2-normalize 過的 embedding 矩陣（shape: [len(texts), dim)]）。
    先 normalize 好，後面 cosine similarity 可以直接用內積算，
    跟 pipeline/vector_store.py 的 Qdrant collection（COSINE distance）行為一致。
    """
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


def encode_one(text: str) -> np.ndarray:
    return encode([text])[0]
