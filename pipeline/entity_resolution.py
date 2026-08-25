"""把不同文章抽取出來、指向同一個真實世界實體的字串（例如「台達電子」vs
「Delta Electronics」）合併到同一個 canonical 名稱，避免 Neo4j 累積重複
Entity 節點。

跟 legacy/pipeline/tag_clustering.py 是同一套「依相似度貪婪指派到最像的既有
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

import numpy as np

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

# 灰色地帶要問 LLM 的候選數上限。
#
# 舊版只問分數最高的那一個候選，實測會漏掉真同義的實體：canonical 池裡同時
# 有「輝達」跟「Delta Electronics」時，查「台達電子」的排名是
#
#     第1名  輝達                0.510   <- 舊版只問這個，LLM 正確回答「不是」
#     第2名  Delta Electronics  0.406   <- 真同義，但永遠問不到
#     第3名  NVIDIA             0.180   <- 落在灰色地帶外
#
# 於是「台達電子」被當成新實體建節點，該合併的沒合併。兩段式判斷解掉了偽
# 陽性（不會錯誤合併），卻換來偽陰性，而觸發條件就是上方 docstring 記錄的
# 那組數字，不是罕見情況。
#
# 改成依分數由高到低問前 K 個，問到第一個確認就停。K 的取捨：K 太小會繼續
# 漏（真同義排在第 K 名之後），K 太大會在「這個實體本來就是新的」時白花 K
# 次呼叫——而新實體在回填初期是多數情況。訂 3 是因為目前觀察到的真同義配
# 對都落在前兩名，留一名餘裕。之後有真實資料可以量「確認發生在第幾名」的
# 分布，再回來調這個數字。
_GREY_ZONE_TOP_K = 3

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _embedding_dim(model) -> int:
    """sentence-transformers 5.x 把 get_sentence_embedding_dimension() 改名成
    get_embedding_dimension()，requirements.txt 容許的下限 3.0 只有舊名稱，
    兩個都試一次。"""
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return getter()


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
        # 全部 canonical 的 embedding 疊成一個 (n, dim) 矩陣，比對時用一次
        # 矩陣乘算完所有相似度。舊版是 Python 迴圈逐一算內積，池子在回填過
        # 程中會一路長大，整批下來是 O(n^2) 的 Python 層運算。
        self._matrix = (
            np.asarray(model.encode(self._canonical_names, normalize_embeddings=True), dtype=np.float32)
            if self._canonical_names
            else np.zeros((0, _embedding_dim(model)), dtype=np.float32)
        )

    def resolve(self, name: str) -> tuple[str, bool]:
        """回傳 (canonical_name, is_new)。is_new=True 代表這個字串沒有找到
        夠像的既有 canonical，自己成為新的 canonical。

        灰色地帶依分數由高到低問前 _GREY_ZONE_TOP_K 個候選，問到第一個確認
        就停；只問第一名會漏掉排在後面的真同義實體（見該常數的註解）。
        """
        model = _get_model()
        embedding = np.asarray(
            model.encode([name], normalize_embeddings=True)[0], dtype=np.float32
        )

        matched = None
        if len(self._canonical_names):
            scores = self._matrix @ embedding

            # 分數夠高直接判定同一實體，不花 LLM 額度。
            top = int(np.argmax(scores))
            if scores[top] >= _GREY_ZONE_UPPER:
                matched = self._canonical_names[top]
            else:
                # 灰色地帶：分數本身不足以下判斷，由高到低問 LLM。
                grey = np.flatnonzero(
                    (scores > _GREY_ZONE_LOWER) & (scores < _GREY_ZONE_UPPER)
                )
                order = grey[np.argsort(-scores[grey])][:_GREY_ZONE_TOP_K]
                for idx in order:
                    candidate = self._canonical_names[int(idx)]
                    if _llm_confirms_same_entity(name, candidate, client=self._llm_client):
                        matched = candidate
                        break

        if matched is not None:
            return matched, False

        self._canonical_names.append(name)
        self._matrix = np.vstack([self._matrix, embedding])
        return name, True
