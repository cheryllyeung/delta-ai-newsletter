"""話題式週報的 reranker：bge-reranker-v2-m3，寫作階段兩段式檢索的第二段
（PRD §6：小 embedding 撈 top-20 + reranker 重排，檢索品質通常優於單用大型
embedding 模型，且資源需求低一個量級）。

2026-08-13 從本機推論改成走公司 gateway。這顆是 cross-encoder，要把 query 跟
每一篇候選文章配對後各跑一次前向傳播，20 篇候選就是 20 次，在筆電 CPU 上是
整條出刊流程最慢的一段（每篇話題數分鐘）。gateway 上有同一顆模型，端點是
OpenAI 規格之外的 POST /v1/rerank（各家自訂路徑不同，這個是實測出來的）。

gateway 掛掉時會退回本機推論，慢但至少出得了刊。
"""
from __future__ import annotations

import os

import requests

_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
_TIMEOUT_SECONDS = 60

_model = None


def _get_model():
    """本機備援用的 CrossEncoder，只有 gateway 失敗時才會載入。"""
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder

        _model = CrossEncoder(_MODEL_NAME)
    return _model


def _scores_via_gateway(query: str, documents: list[str]) -> list[float] | None:
    """回傳跟 documents 同順序的分數；gateway 不可用就回 None 讓呼叫端退回本機。"""
    base = (os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    api_key = os.environ.get("LLM_API_KEY")
    if not base or not api_key:
        return None
    try:
        response = requests.post(
            f"{base}/rerank",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": _MODEL_NAME, "query": query, "documents": documents},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()["results"]
    except Exception as exc:  # noqa: BLE001 -- 退回本機，不要讓檢索整個失敗
        print(f"[reranker] gateway 不可用，改用本機推論（會慢很多）：{exc}")
        return None

    # 回傳是依分數排序的，要照 index 放回原順序。
    scores = [0.0] * len(documents)
    for item in results:
        scores[item["index"]] = item["relevance_score"]
    return scores


def _scores(query: str, documents: list[str]) -> list[float]:
    gateway_scores = _scores_via_gateway(query, documents)
    if gateway_scores is not None:
        return gateway_scores
    return list(_get_model().predict([(query, doc) for doc in documents]))


def rerank(query: str, documents: list[str], top_k: int, min_score: float = 0.0) -> list[int]:
    """回傳 documents 依跟 query 相關度由高到低排序後的原始 index 清單，
    只取前 top_k 個，且分數低於 min_score 的一律不回傳。

    min_score 是必要的：只取前 top_k 名會「湊滿名額」，話題底下只有一篇真正
    相關的文章時，第二個名額還是會被填上，填什麼都好。2026-08-11 實測就出現
    過一篇講 Docker Sandboxes 的文章被塞進一個完全不相干的 GitHub repo 當
    素材，然後寫作真的引用了那個 repo 的 star 數當數據。

    分數是 sigmoid 機率（0~1），實測分布是雙峰的：真正相關的落在 0.8~1.0，
    其餘直接掉到 0.16 以下，中間幾乎是空的，所以門檻很好訂。

    全部低於 min_score 時回傳空清單。這裡原本有一條保底「至少留最相關的
    那一篇，不要讓寫作完全沒有素材」，2026-08-14 拿掉了：那條保底是舊架構
    的產物，當時素材完全靠全池檢索，撈不到就真的沒東西可寫。現在素材主體
    是話題自己的文章（見 pipeline/retrieval.py），這支只負責排序跟補充，
    保底留著只會讓補充素材的門檻形同虛設：不管分數多低都一定會塞一篇進來，
    那正是「標題跟內容對不上」的成因。
    """
    if not documents:
        return []
    scores = _scores(query, documents)
    ranked = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
    return [i for i in ranked[:top_k] if scores[i] >= min_score]
