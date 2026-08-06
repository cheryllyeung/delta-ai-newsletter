"""知識圖譜階段一：對每篇已標籤文章抽取 OpenIE 風格三元組
（prompts/triple_extraction.md），做為圖建構的來源資料。

跟 pipeline/article_tagging.py 完全脫鉤：只負責這一次 LLM 呼叫本身，
不寫入任何資料庫。三元組的實體解析（pipeline/entity_resolution.py）跟
寫入 Neo4j（pipeline/graph_store.py）都是呼叫端（scripts/ingest_topics.py）
的責任，跟 tag_article() 只負責標籤抽取、不管持久化是同一個分工方式。
"""
from __future__ import annotations

import json

from ingestion.base import RawItem
from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_CONTENT_CHARS_FOR_EXTRACTION = 6000  # 跟 article_tagging.py 用同一個量級


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def extract_triples(item: RawItem, client=None) -> dict:
    """對單篇文章抽取三元組，回傳 {"triples": [{"subject":..., "relation":..., "object":...}, ...]}。"""
    client = client or get_client()

    system, user = load_prompt_parts(
        "triple_extraction",
        title=item.title,
        published_date=item.published_at.date().isoformat(),
        content=item.summary[:_CONTENT_CHARS_FOR_EXTRACTION],
    )

    response = create_chat_completion(
        client,
        model=get_model(),
        max_tokens=4000,  # 三元組數量沒上限，比單一 tagging JSON 略寬
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **reasoning_effort_kwargs(),
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    log_call("triple_extraction", system, user, raw_text, parsed)
    return parsed
