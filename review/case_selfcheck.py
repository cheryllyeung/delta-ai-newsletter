"""Prompt 4：發布前自檢，獨立呼叫（不帶生成階段的對話歷史），temperature=0。

刻意用全新的 client.messages.create() 呼叫，只餵生成結果＋事實清單＋原文，
不讓模型看到自己剛剛是怎麼被要求寫這篇文章的，避免它替自己的產出護航。
"""
from __future__ import annotations

import json
import os

import anthropic

from pipeline.llm_client import get_client
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

MODEL = os.environ.get("NEWSLETTER_MODEL", "claude-sonnet-5")

_SOURCE_CHARS_FOR_CHECK = 8000


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def _compute_confidence(result: dict) -> float:
    claims = result.get("fact_claims", [])
    supported_ratio = (
        sum(1 for c in claims if c.get("verdict") == "supported") / len(claims)
        if claims
        else 1.0
    )
    style_ok = 1.0 if not result.get("style_violations") else 0.5
    sensitivity_ok = 1.0 if not result.get("sensitivity_flags") else 0.0
    return 0.6 * supported_ratio + 0.2 * style_ok + 0.2 * sensitivity_ok


def self_check(
    generated_article: dict,
    key_facts: list[dict],
    source_content: str,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """回傳規格書定義的審查結果，並附上 pipeline 端算好的 confidence。"""
    client = client or get_client()

    system, user = load_prompt_parts(
        "self_check",
        generated_article_json=json.dumps(generated_article, ensure_ascii=False, indent=2),
        key_facts=json.dumps(key_facts, ensure_ascii=False, indent=2),
        source_content=source_content[:_SOURCE_CHARS_FOR_CHECK],
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    parsed = _parse_json_object(raw_text)
    parsed["confidence"] = _compute_confidence(parsed)
    log_call("self_check", system, user, raw_text, parsed)
    return parsed
