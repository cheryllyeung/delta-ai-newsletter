"""階段四：把入選話題（含 reranker 檢索出的 top-5 素材）生成成文章 JSON。

跟 generation/case_generate.py 是平行模組（那支服務 Delta Pulse 案例式
週報），JSON 解析容錯寫法沿用同一套慣例。
"""
from __future__ import annotations

import json
import random
import sqlite3

import openai

from pipeline.llm_client import create_chat_completion, get_client, get_writing_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts
from pipeline.text_normalize import fix_stray_simplified_in

_CONTENT_CHARS_PER_SOURCE = 1500

_CONTENT_TYPE_NAMES = {
    "insight": "洞見型",
    "practical": "實用型",
    "warning": "警示型",
    "flash": "快訊型",
}

# 開頭手法清單，跟 prompts/style_guide.md 的【第一段怎麼開頭】保持一致。
# 每篇話題是獨立的 LLM 呼叫，彼此看不到對方選了什麼手法，實測發現放給
# LLM 自己每篇挑的話，幾乎每篇都收斂成「情境假設句」（開頭都是「你...」），
# 「不要每篇都選同一種」這條指示在多篇獨立呼叫下沒辦法自己生效。改成由
# 呼叫端（scripts/compose_topic_issue.py）洗牌後輪流指派，同一期裡儘量
# 不重複；沒有明確指派時（例如單篇測試呼叫）就隨機挑一種，至少不會每次
# 都預設同一種。
OPENING_TECHNIQUES = [
    "情境假設句（把讀者放進一個具體處境裡）",
    "反差對比（兩個數字或兩個做法的落差）",
    "具體畫面比喻",
    "對讀者的犀利提問",
    "意外的數字或事實開場",
]


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


def build_sources_text(source_rows: list[sqlite3.Row]) -> str:
    """比照 sources_text 給寫作 prompt 用的格式，組出來源全文區塊。
    review/topic_selfcheck.py 的自檢也重用這支，確保審核跟寫作看到的
    是同一份來源文字，不會因為格式不同而誤判。"""
    blocks = []
    for row in source_rows:
        blocks.append(
            f"【來源：{row['source_name']}】\n"
            f"標題：{row['title']}\n"
            f"連結：{row['url']}\n"
            f"內文：{row['content'][:_CONTENT_CHARS_PER_SOURCE]}"
        )
    return "\n\n".join(blocks)


def generate_topic_article(
    newsletter_name: str,
    topic_title: str,
    content_type: str,
    source_rows: list[sqlite3.Row],
    revision_instructions: str | None = None,
    client: openai.OpenAI | None = None,
    opening_technique: str | None = None,
) -> dict:
    """依入選話題與 reranker 檢索出的來源文章清單生成文章。

    source_rows 是 pipeline.topic_db 的 articles 列，經
    pipeline.retrieval.retrieve_sources_for_topic() 撈出的 top-5，順序即為
    reranker 排序（相關度由高到低）。
    """
    client = client or get_client()
    opening_technique = opening_technique or random.choice(OPENING_TECHNIQUES)

    revision_note = ""
    if revision_instructions:
        revision_note = (
            "\n【上一版審查未通過，請針對以下具體問題修訂，不要整篇重寫】\n"
            f"{revision_instructions}"
        )

    system, user = load_prompt_parts(
        "topic_generate",
        newsletter_name=newsletter_name,
        topic_title=topic_title,
        content_type=content_type,
        content_type_name=_CONTENT_TYPE_NAMES[content_type],
        sources_text=build_sources_text(source_rows),
        revision_note=revision_note,
        opening_technique=opening_technique,
    )

    response = create_chat_completion(
        client,
        model=get_writing_model(),
        max_tokens=2500,
        temperature=0.7,
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
        log_call("topic_generate", system, user, raw_text, None)
        raise
    # 保險絲：LLM 偶爾會在繁體輸出裡夾雜簡體字，這裡逐字元修正掉。
    parsed = fix_stray_simplified_in(parsed)
    log_call("topic_generate", system, user, raw_text, parsed)
    return parsed
