"""實體解析煙霧測試：驗證灰色地帶的候選挑選邏輯，以及門檻常數所依據的
cosine 數字有沒有位移。

跟 tests/smoke_test_graph_extraction.py 同一種哲學（真實輸入、只 mock 會
花錢的 LLM 呼叫），但刻意連 Neo4j 都不碰：EntityResolver 的建構子收的是
list[str]，Neo4j 只在上游供名單、下游寫節點，解析邏輯本身跟圖資料庫無關。
所以這支不需要 .env、不需要 docker、不需要網路，隨時可以跑。

為什麼 mock 掉 LLM 而不是真的打一次：這裡要防的失效模式是「灰色地帶挑錯
候選」，不是「LLM 答錯」。2026-08-07 那個 bug 就是純粹的前者：當時 LLM
每一次都答對了，錯的是程式只拿分數最高的那一個去問。把兩者綁在一起會讓
這支測試在換模型或網路不穩時變紅，久了就會被當成雜訊忽略，等於沒有測試。
LLM 本身答得對不對，靠 runs/entity_match_check/ 的留痕事後稽核。

用法：
    python -m tests.smoke_test_entity_resolution
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import entity_resolution as er

# pipeline/entity_resolution.py 的 docstring 記錄的實測值。_GREY_ZONE_LOWER、
# _GREY_ZONE_UPPER、_GREY_ZONE_TOP_K 三個常數全部是照這組數字校準的，一旦
# sentence-transformers 升版讓分數位移，三個常數會同時失效而且不會有任何
# 錯誤訊息（解析結果只是默默變差），所以把數字釘在這裡當回歸基準。
# 2026-08-10 在 Windows + sentence-transformers 5.7.0 重跑，六組全部吻合到
# 小數第三位，所以容許誤差抓 0.005 就夠，不用放寬。
_COSINE_TOLERANCE = 0.005
_EXPECTED_COSINE = [
    ("台達電子", "Delta Electronics", 0.406, "真同義，跨語言"),
    ("台達電子", "台達", 0.496, "真同義，簡稱"),
    ("台達電子", "輝達", 0.510, "不同公司，純粹共用「達」字"),
    ("NVIDIA", "輝達", 0.258, "真同義，跨語言"),
    ("NVIDIA", "Nvidia Corporation", 0.892, "真同義，同語言變體"),
    ("台達電子", "NVIDIA", 0.180, "不相關"),
]

# 0807 報告記錄的 canonical 池：查「台達電子」時「輝達」(0.510) 會排在真
# 同義的「Delta Electronics」(0.406) 前面，這正是觸發 bug 的排列。
_CANONICAL_POOL = ["輝達", "Delta Electronics", "NVIDIA"]

# mock 的 LLM oracle：把正確答案寫成查表。刻意對沒列到的配對丟例外而不是
# 回 False。如果候選挑選邏輯被改動、問了預期外的配對，這裡要大聲失敗，
# 而不是安靜地當成「不同實體」讓測試矇混過關。
_ORACLE = {
    frozenset(("台達電子", "Delta Electronics")): True,
    frozenset(("台達電子", "台達")): True,
    frozenset(("台達電子", "輝達")): False,
    frozenset(("NVIDIA", "輝達")): True,
}


def _fake_llm(name_a: str, name_b: str, client=None) -> bool:
    key = frozenset((name_a, name_b))
    if key not in _ORACLE:
        raise AssertionError(
            f"候選挑選邏輯問了預期外的配對：「{name_a}」vs「{name_b}」。"
            f"若這是刻意的行為變更，請一併更新 _ORACLE。"
        )
    return _ORACLE[key]


def _resolve(pool: list[str], query: str, top_k: int | None = None) -> tuple[str, bool, list[tuple[str, str]]]:
    """跑一次 resolve()，回傳 (canonical, is_new, 實際問過的配對清單)。"""
    asked: list[tuple[str, str]] = []

    def recording(name_a, name_b, client=None):
        asked.append((name_a, name_b))
        return _fake_llm(name_a, name_b, client=client)

    original_top_k = er._GREY_ZONE_TOP_K
    if top_k is not None:
        er._GREY_ZONE_TOP_K = top_k
    try:
        with patch.object(er, "_llm_confirms_same_entity", recording):
            canonical, is_new = er.EntityResolver(pool).resolve(query)
    finally:
        er._GREY_ZONE_TOP_K = original_top_k
    return canonical, is_new, asked


def check_cosine_baseline() -> bool:
    print("[1/2] 門檻常數所依據的 cosine 數字")
    model = er._get_model()
    ok = True
    for name_a, name_b, expected, note in _EXPECTED_COSINE:
        va, vb = model.encode([name_a, name_b], normalize_embeddings=True)
        actual = float(np.dot(va, vb))
        drifted = abs(actual - expected) > _COSINE_TOLERANCE
        ok = ok and not drifted
        status = "位移" if drifted else "符合"
        print(
            f"      {status}  {name_a} vs {name_b}：基準 {expected:.3f}、"
            f"實測 {actual:.3f}（{note}）"
        )
    if not ok:
        print(
            "      cosine 已位移，pipeline/entity_resolution.py 的 _GREY_ZONE_LOWER／"
            "_GREY_ZONE_UPPER／_GREY_ZONE_TOP_K 需要重新校準。"
        )
    return ok


def check_resolution_cases() -> bool:
    print("[2/2] 灰色地帶的候選挑選")
    results = []

    # A：真同義排在第二名也要找得到。這是 0807 修正的目標。
    canonical, is_new, asked = _resolve(_CANONICAL_POOL, "台達電子")
    results.append((
        canonical == "Delta Electronics" and not is_new,
        f"真同義排第二名仍合併成功：得到 {canonical!r}、問了 {len(asked)} 次",
    ))

    # B：把 TOP_K 壓回 1 應該重現舊 bug。這一項顯示的是「修正確實有效」而
    # 不是「碰巧會過」。沒有這個對照，A 過了也不知道是不是修正的功勞。
    canonical, is_new, asked = _resolve(_CANONICAL_POOL, "台達電子", top_k=1)
    results.append((
        is_new is True,
        f"TOP_K=1 重現 0807 的偽陰性：得到 {canonical!r}、is_new={is_new}",
    ))

    # C：修掉偽陰性不能換來偽陽性——池子裡只有字面雷同的假配對時，必須
    # 判定為新實體。這是兩段式判斷原本就守住的防線，不可以退化。
    canonical, is_new, asked = _resolve(["輝達"], "台達電子")
    results.append((
        is_new is True,
        f"只有假配對時不錯誤合併：得到 {canonical!r}、is_new={is_new}",
    ))

    # D：分數高於上界直接判定，不花 LLM 額度。
    canonical, is_new, asked = _resolve(["NVIDIA"], "Nvidia Corporation")
    results.append((
        not is_new and len(asked) == 0,
        f"高分捷徑略過 LLM：得到 {canonical!r}、問了 {len(asked)} 次",
    ))

    for passed, message in results:
        print(f"      {'通過' if passed else '失敗'}  {message}")
    return all(passed for passed, _ in results)


def main() -> None:
    print("[smoke_test_entity_resolution] 開始（不需要 .env／Neo4j／網路）")
    ok = check_cosine_baseline()
    ok = check_resolution_cases() and ok
    print(f"[smoke_test_entity_resolution] {'全部通過' if ok else '有項目失敗'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
