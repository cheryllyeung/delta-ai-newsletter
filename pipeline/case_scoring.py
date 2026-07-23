"""Prompt 1：對候選案例文章評分、分類、開放式標籤（不做淘汰判斷）。

v2（pool 化架構）：這裡不再有 select_cases()。評分完的文章一律寫進
pipeline/pool_db.py 管理的長期文章池，「這期要選誰」是另外一支模組
（pipeline/pool_selection.py）對 pool 下查詢決定的，跟評分這一步完全脫鉤。
"""
from __future__ import annotations

import json
import os

from ingestion.base import RawItem
from pipeline.llm_client import get_client
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

# 內文送進評分 prompt 前的截斷長度（純粹是控制 token 成本）。
_CONTENT_CHARS_FOR_SCORING = 6000


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def score_article(item: RawItem, client=None) -> dict:
    """評分單篇文章，回傳 Prompt 1 的完整解析結果。

    不做任何淘汰判斷：is_ai_application=false 的文章一樣回傳完整評分，
    由呼叫端（scripts/ingest_pool.py）決定怎麼處理，這支函式本身不丟棄任何東西。
    """
    client = client or get_client()
    source_name = item.extra.get("source_name", item.subdomain_id)
    source_weight = item.extra.get("source_weight", item.score)

    system, user = load_prompt_parts(
        "scoring",
        source_name=source_name,
        source_weight=source_weight,
        title=item.title,
        published_date=item.published_at.date().isoformat(),
        content=item.summary[:_CONTENT_CHARS_FOR_SCORING],
    )

    response = client.messages.create(
        model=os.environ.get("NEWSLETTER_MODEL", "claude-sonnet-5"),
        max_tokens=1500,
        temperature=0.2,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    parsed = _parse_json_object(raw_text)
    log_call("scoring", system, user, raw_text, parsed)
    return parsed


def compute_base_score(
    scores: dict,
    weights: dict,
    source_weight: float,
    is_ai_application: bool,
    non_application_multiplier: float = 0.2,
) -> float:
    """算排序用的基礎分數（尚未套趨勢加成，趨勢加成在選文查詢時才算，
    因為趨勢會隨 pool 每天有新文章而變動，評分當下算好會過時）。

    is_ai_application=false 不淘汰，只把分數打折，讓文章幾乎不會被選到、
    但仍然留在 pool 裡，供之後真的需要湊數或趨勢反轉時使用。
    """
    weighted_sum = (
        weights["transferability"] * scores["transferability"]
        + weights["specificity"] * scores["specificity"]
        + weights["novelty"] * scores["novelty"]
        + weights["narrativity"] * scores["narrativity"]
    )
    base_score = source_weight * weighted_sum
    if not is_ai_application:
        base_score *= non_application_multiplier
    return base_score
