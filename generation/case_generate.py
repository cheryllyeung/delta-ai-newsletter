"""Prompt 2：把一篇入選案例文章生成成 400-600 字的案例內頁 JSON。

跟 generation/summarize.py 是平行模組（那支服務舊的趨勢彙整 pipeline，
這支服務 Delta Pulse 案例式週報），JSON 解析容錯寫法沿用同一套慣例。
"""
from __future__ import annotations

import json
import os

import anthropic

from pipeline.llm_client import get_client
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

MODEL = os.environ.get("NEWSLETTER_MODEL", "claude-sonnet-5")

_CONTENT_CHARS_FOR_GENERATION = 8000


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def generate_case(
    newsletter_name: str,
    article_row,
    tags: list[str],
    key_facts: list[dict],
    revision_instructions: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """依文章池裡的一列資料（pipeline.pool_db 的 articles row）生成案例內頁。

    article_row 是 sqlite3.Row，至少要有 title/source_name/url/content/
    area_category 欄位。revision_instructions 有值時，代表這是自檢未過後的
    重新生成，會把審查員給的具體修訂指令附加進 user prompt，要求針對性
    修正而不是整篇重寫。
    """
    client = client or get_client()

    revision_note = ""
    if revision_instructions:
        revision_note = (
            "\n【上一版審查未通過，請針對以下具體問題修訂，不要整篇重寫】\n"
            f"{revision_instructions}"
        )

    system, user = load_prompt_parts(
        "generate",
        newsletter_name=newsletter_name,
        area_category=article_row["area_category"],
        hashtags="、".join(tags),
        title=article_row["title"],
        source_name=article_row["source_name"],
        url=article_row["url"],
        key_facts=json.dumps(key_facts, ensure_ascii=False, indent=2),
        content=article_row["content"][:_CONTENT_CHARS_FOR_GENERATION],
        revision_note=revision_note,
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        temperature=0.7,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    parsed = _parse_json_object(raw_text)
    log_call("generate", system, user, raw_text, parsed)
    return parsed
