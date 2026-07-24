"""階段四：把入選話題（含 reranker 檢索出的 top-5 素材）生成成文章 JSON。

跟 generation/case_generate.py 是平行模組（那支服務 Delta Pulse 案例式
週報），JSON 解析容錯寫法沿用同一套慣例。
"""
from __future__ import annotations

import json
import sqlite3

import openai

from pipeline.llm_client import get_client, get_model
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_CONTENT_CHARS_PER_SOURCE = 4000

_CONTENT_TYPE_NAMES = {
    "insight": "洞見型",
    "practical": "實用型",
    "warning": "警示型",
    "flash": "快訊型",
}


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
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
) -> dict:
    """依入選話題與 reranker 檢索出的來源文章清單生成文章。

    source_rows 是 pipeline.topic_db 的 articles 列，經
    pipeline.retrieval.retrieve_sources_for_topic() 撈出的 top-5，順序即為
    reranker 排序（相關度由高到低）。
    """
    client = client or get_client()

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
    )

    response = client.chat.completions.create(
        model=get_model(),
        max_tokens=5000,
        temperature=0.7,
        reasoning_effort="low",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    log_call("topic_generate", system, user, raw_text, parsed)
    return parsed
