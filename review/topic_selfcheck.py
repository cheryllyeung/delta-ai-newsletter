"""階段五審核前的自檢：獨立呼叫（不帶生成階段的對話歷史），temperature=0。

跟 legacy/review/case_selfcheck.py 是平行模組，同樣的獨立呼叫設計理由：只餵生成
結果＋來源全文，不讓模型看到自己剛剛是怎麼被要求寫這篇文章的，避免它
替自己的產出護航。
"""
from __future__ import annotations

import json
import re
import sqlite3

import openai

from generation.topic_generate import build_sources_text
from pipeline.llm_client import create_chat_completion, get_client, get_review_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

# 自檢能看到的來源全文長度。2026-09-21 從 3000 提到 45000：原本審查員
# 看到的來源比寫作端還少（而且是整批截斷，第二篇之後的來源可能整篇被
# 切掉），寫作端誇大了後半段原文它根本無從發現。現在寫作端每篇來源餵
# 8000 字（見 generation/topic_generate.py），這裡要涵蓋寫作看過的全部，
# 45000 = 5 篇 × 8000 加上格式框架的餘裕。
_SOURCE_CHARS_FOR_CHECK = 45000


def _parse_json_object(raw_text: str) -> dict:
    # strict=False：LLM 常在字串值裡直接吐出沒跳脫的換行/tab 等控制字元，
    # 嚴格模式的 json.loads 會直接拋 JSONDecodeError（Invalid control character）。
    try:
        return json.loads(raw_text, strict=False)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1], strict=False)
        raise


# 連貫性沒過時信心度的上限。
#
# 這是硬性上限不是扣分：標題跟內文對不上、或整篇是兩三件事硬接在一起，
# 不是「品質差一點」而是「這篇不該出」，不能因為引用都有依據、風格也沒
# 違規就把分數拉回門檻之上。0.4 壓在 config 的 regenerate_below（0.6）
# 之下，這樣一定會走重寫；重寫兩次還是不過就會被標成待人工確認。
#
# 2026-08-14 加的，起因是同仁反映「標題跟內容對不上，像硬把幾個來源湊成
# 一篇」。當時 26 篇已出刊文章的信心度都在門檻之上，因為舊公式只看事實
# 有沒有依據，那些不相干的素材確實都是真的，只是它們講的不是同一件事。
_INCOHERENT_CONFIDENCE_CAP = 0.4


def is_coherent(result: dict) -> bool:
    """這篇是不是在講同一件事：標題對得上內文、整篇圍繞一個主軸、沒有把
    不相干的來源寫進去。scripts/compose_topic_issue.py 用這支決定重寫兩次
    之後還是不連貫的文章要不要出刊。

    舊資料跟舊 prompt 沒有 coherence_check 欄位，讀不到時當作通過，不要讓
    補跑舊資料時整批被判不合格。"""
    coherence = result.get("coherence_check")
    if not isinstance(coherence, dict):
        return True
    if coherence.get("headline_matches_body") is False:
        return False
    if coherence.get("single_subject") is False:
        return False
    return not coherence.get("unrelated_sources")


# 生成結果只該有繁體中文與英文專有名詞。2026-09-21 加：實測出過內文混進
# 俄文（「分子 профиля」），LLM 自檢沒抓到就照登了。西里爾字母、假名、
# 諺文用程式直接掃，比要求審查員「注意看」可靠。簡體字另有
# pipeline/text_normalize.py 逐字元轉掉，不歸這裡管。
_FOREIGN_CHARS = re.compile(r"[Ѐ-ӿ぀-ヿ가-힯]")

# 抓到殘留字元時信心度的上限。壓在 regenerate_below（0.8）之下，一定會
# 觸發重寫；重寫指示會附上出現位置。
_FOREIGN_RESIDUE_CONFIDENCE_CAP = 0.7


def _foreign_char_violations(value, path: str = "") -> list[dict]:
    if isinstance(value, str):
        m = _FOREIGN_CHARS.search(value)
        if m:
            snippet = value[max(0, m.start() - 10): m.end() + 10]
            return [{"rule": "非中英文字元殘留", "location": f"{path}：…{snippet}…"}]
        return []
    if isinstance(value, list):
        found = []
        for i, v in enumerate(value):
            found.extend(_foreign_char_violations(v, f"{path}[{i}]"))
        return found
    if isinstance(value, dict):
        found = []
        for k, v in value.items():
            found.extend(_foreign_char_violations(v, f"{path}.{k}" if path else k))
        return found
    return []


def _compute_confidence(result: dict) -> float:
    claims = result.get("fact_claims", [])
    supported_ratio = (
        sum(1 for c in claims if c.get("verdict") == "supported") / len(claims)
        if claims
        else 1.0
    )
    style_ok = 1.0 if not result.get("style_violations") else 0.5
    sensitivity_ok = 1.0 if not result.get("sensitivity_flags") else 0.0
    confidence = 0.6 * supported_ratio + 0.2 * style_ok + 0.2 * sensitivity_ok
    if not is_coherent(result):
        return min(confidence, _INCOHERENT_CONFIDENCE_CAP)
    return confidence


def self_check(
    generated_article: dict,
    source_rows: list[sqlite3.Row],
    client: openai.OpenAI | None = None,
) -> dict:
    """回傳審查結果，並附上 pipeline 端算好的 confidence（跟 case pipeline
    同一套公式，不採信模型自己輸出的信心分數，避免它對自己過度樂觀）。
    """
    client = client or get_client()

    sources_text = build_sources_text(source_rows)[:_SOURCE_CHARS_FOR_CHECK]
    system, user = load_prompt_parts(
        "topic_self_check",
        generated_article_json=json.dumps(generated_article, ensure_ascii=False, indent=2),
        sources_text=sources_text,
    )

    response = create_chat_completion(
        client,
        model=get_review_model(),
        max_tokens=4000,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **reasoning_effort_kwargs(),
    )
    raw_text = response.choices[0].message.content
    try:
        parsed = _parse_json_object(raw_text)
    except json.JSONDecodeError:
        # 解析失敗也要把原始回應存下來，不然沒辦法回頭比對到底是哪裡壞的。
        log_call("topic_self_check", system, user, raw_text, None)
        raise
    foreign = _foreign_char_violations(generated_article)
    if foreign:
        parsed.setdefault("style_violations", []).extend(foreign)
        locations = "；".join(v["location"] for v in foreign)
        parsed["revision_instructions"] = (
            f"{parsed.get('revision_instructions', '')}\n"
            f"內文混入了非中英文的字元，重寫時改成正確的繁體中文：{locations}"
        ).strip()
    parsed["confidence"] = _compute_confidence(parsed)
    if foreign:
        parsed["confidence"] = min(parsed["confidence"], _FOREIGN_RESIDUE_CONFIDENCE_CAP)
    log_call("topic_self_check", system, user, raw_text, parsed)
    return parsed
