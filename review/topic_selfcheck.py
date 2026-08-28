"""階段五審核前的自檢：獨立呼叫（不帶生成階段的對話歷史），temperature=0。

跟 legacy/review/case_selfcheck.py 是平行模組，同樣的獨立呼叫設計理由：只餵生成
結果＋來源全文，不讓模型看到自己剛剛是怎麼被要求寫這篇文章的，避免它
替自己的產出護航。
"""
from __future__ import annotations

import json
import sqlite3

import openai

from generation.topic_generate import build_sources_text
from pipeline.llm_client import create_chat_completion, get_client, get_review_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_SOURCE_CHARS_FOR_CHECK = 3000


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
    parsed["confidence"] = _compute_confidence(parsed)
    log_call("topic_self_check", system, user, raw_text, parsed)
    return parsed
