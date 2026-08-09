"""用本機 embedding 模型把語意相同、寫法不同的 hashtag 自動合併成同一個
「canonical」寫法，解決開放式標籤（沒有詞庫限制）造成的同義詞問題：
「AI客服」跟「智能客服機器人」應該被算成同一個趨勢，不是兩個獨立的詞。

刻意選本機模型（sentence-transformers），不打任何外部 API：
- 不依賴目前還沒打通的 Clawith gateway
- 免費、無次數限制、結果可重現
- 第一次執行會自動下載模型權重（~百MB），之後離線可用

跟評分（pipeline/case_scoring.py）完全脫鉤：這裡不影響文章怎麼被打標籤，
只負責事後「把已經存在的標籤重新分組」，寫進 pipeline/pool_db.py 的
tag_aliases 表。分組結果由 scripts/ingest_pool.py 在每次抓取完自動觸發，
不需要人工手動跑。
"""
from __future__ import annotations

from collections import Counter

_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
# 實測：真正同義的中文詞組（"AI客服" vs
# "智能客服機器人"）cosine similarity 約 0.6，不相關的詞約 0.25-0.36，
# 門檻設在中間，沒有假設它會跟英文語料一樣普遍落在 0.75 以上。
_SIMILARITY_THRESHOLD = 0.5

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def compute_tag_clusters(tag_counts: dict[str, int]) -> dict[str, str]:
    """把標籤依語意相似度分組，回傳 {原始標籤: canonical標籤} 的完整映射
    （包含「自己就是canonical」的項目，方便呼叫端直接整批覆寫）。

    分組邏輯：依出現次數由高到低排序，愈常出現的標籤愈優先成為某個群組的
    canonical 代表（比較穩定、比較不會因為新標籤加入就一直改名）。逐一比對
    每個標籤跟目前已存在的 canonical 代表的相似度，超過門檻就併進去，
    否則自己成為新的 canonical 代表。
    """
    tags = sorted(tag_counts.keys(), key=lambda t: tag_counts[t], reverse=True)
    if not tags:
        return {}
    if len(tags) == 1:
        return {tags[0]: tags[0]}

    model = _get_model()
    embeddings = model.encode(tags, normalize_embeddings=True)

    canonical_tags: list[str] = []
    canonical_embeddings = []
    mapping: dict[str, str] = {}

    for tag, embedding in zip(tags, embeddings):
        best_match, best_score = None, 0.0
        for canonical, canonical_embedding in zip(canonical_tags, canonical_embeddings):
            score = float(embedding @ canonical_embedding)
            if score > best_score:
                best_match, best_score = canonical, score

        if best_match is not None and best_score >= _SIMILARITY_THRESHOLD:
            mapping[tag] = best_match
        else:
            canonical_tags.append(tag)
            canonical_embeddings.append(embedding)
            mapping[tag] = tag

    return mapping


def tag_counts_from_pool(conn) -> dict[str, int]:
    """統計 pool 裡每個原始標籤（尚未經過別名合併）目前出現幾次，
    給 compute_tag_clusters() 排優先序用。
    """
    rows = conn.execute("SELECT tag FROM article_tags").fetchall()
    return dict(Counter(row["tag"] for row in rows))
