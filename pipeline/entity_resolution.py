"""把不同文章抽取出來、指向同一個真實世界實體的字串（例如「台達電子」vs
「Delta Electronics」）合併到同一個 canonical 名稱，避免 Neo4j 累積重複
Entity 節點。

跟 pipeline/tag_clustering.py 是同一套「依相似度貪婪指派到最像的既有
canonical」演算法，但改成串流式：既有 canonical（從 Neo4j 讀回來）先當
種子，本次 run 新出現的實體字串逐一比對、合併或新增，不會重新洗牌既有
canonical 名稱。tag_clustering.py 的 compute_tag_clusters() 每次都是從零
重算整個映射，這對「只是查表、沒有持久身份」的標籤別名沒問題，但對圖
節點不安全：一旦 Neo4j 裡的 Entity 節點已經連好邊，重算可能把 canonical
換掉，舊邊就對不上新 canonical，等於斷掉 provenance。

單純套用 tag_clustering.py 那組門檻（0.5）實測在實體專有名詞上不可靠，
拿真實公司名稱測出以下 cosine similarity（paraphrase-multilingual-MiniLM-
L12-v2）：

    台達電子 vs Delta Electronics（真同義，跨語言）        0.406
    台達電子 vs 台達           （真同義，簡稱）             0.496
    台達電子 vs 輝達           （不同公司，純粹共用「達」字） 0.510
    NVIDIA   vs 輝達           （真同義，跨語言）           0.258
    NVIDIA   vs Nvidia Corporation（真同義，同語言變體）    0.892

問題不是門檻數字沒調好：真同義的跨語言配對（0.406）比不相關但共用一個字
的假配對（0.510）分數還低，單一門檻切不開這兩種情況。這個模型能處理
「同語言拼寫變體」（NVIDIA vs Nvidia Corporation 準確分開），處理不了
「跨語言的公司中英文名稱對應」——而這正是我們最需要的場景。

改用「灰色地帶才問 LLM」的兩段式判斷：分數夠低（_GREY_ZONE_LOWER 以下）
直接判定不同實體，分數夠高（_GREY_ZONE_UPPER 以上）直接判定同一實體，
這兩段都不花 LLM 額度；落在中間的灰色地帶（涵蓋上面表格裡全部的真同義
跨語言配對跟那個假陽性配對）才多打一次 LLM 問「這兩個是不是同一個實體」，
用常識判斷字面雷同但不同公司的陷阱（見 prompts/entity_match_check.md）。
"""
from __future__ import annotations

import json

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# 灰色地帶邊界依上方 docstring 的實測數字訂：真負例（不相關公司）目前觀察
# 到的上限約 0.18（例如「台達電子」vs「NVIDIA」= 0.180），下界留一點餘裕
# 訂在 0.15；真同語言變體（NVIDIA vs Nvidia Corporation）落在 0.892，上界
# 留餘裕訂在 0.85。灰色地帶涵蓋了所有觀察到的跨語言真同義配對（0.258～
# 0.496）跟那個字面雷同的假陽性配對（0.510），這是刻意設計，不是碰巧。
_GREY_ZONE_LOWER = 0.15
_GREY_ZONE_UPPER = 0.85

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def _llm_confirms_same_entity(name_a: str, name_b: str, client=None) -> bool:
    """灰色地帶用：問 LLM 這兩個名稱是不是同一個真實世界實體。"""
    client = client or get_client()
    system, user = load_prompt_parts("entity_match_check", name_a=name_a, name_b=name_b)
    response = create_chat_completion(
        client,
        model=get_model(),
        max_tokens=200,
        temperature=0.0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **reasoning_effort_kwargs(),
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    log_call("entity_match_check", system, user, raw_text, parsed)
    return bool(parsed["same_entity"])


class EntityResolver:
    """單次 ingest run 內共用一個 instance：用既有 canonical 名稱（從
    pipeline/graph_store.py::get_all_canonical_entity_names() 讀回來）當種子，
    這個 run 處理到的每個實體字串都跟目前累積的 canonical 集合比對。"""

    def __init__(self, existing_canonical_names: list[str], llm_client=None):
        model = _get_model()
        self._llm_client = llm_client
        self._canonical_names = list(existing_canonical_names)
        self._canonical_embeddings = (
            list(model.encode(self._canonical_names, normalize_embeddings=True))
            if self._canonical_names else []
        )

    def resolve(self, name: str) -> tuple[str, bool]:
        """回傳 (canonical_name, is_new)。is_new=True 代表這個字串沒有找到
        夠像的既有 canonical，自己成為新的 canonical。"""
        model = _get_model()
        embedding = model.encode([name], normalize_embeddings=True)[0]

        best_match, best_score = None, 0.0
        for canonical, canonical_embedding in zip(self._canonical_names, self._canonical_embeddings):
            score = float(embedding @ canonical_embedding)
            if score > best_score:
                best_match, best_score = canonical, score

        is_match = False
        if best_match is not None:
            if best_score >= _GREY_ZONE_UPPER:
                is_match = True
            elif best_score > _GREY_ZONE_LOWER:
                # 灰色地帶：分數本身不足以下判斷，問 LLM。
                is_match = _llm_confirms_same_entity(name, best_match, client=self._llm_client)

        if is_match:
            return best_match, False

        self._canonical_names.append(name)
        self._canonical_embeddings.append(embedding)
        return name, True
