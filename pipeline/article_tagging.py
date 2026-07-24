"""階段二：對每篇文章抽取多維關鍵詞標籤與案例標記（prompts/article_tagging.md）。

跟話題聚類（pipeline/topic_clustering.py）完全脫鉤：聚類只看向量相似度，
不需要跑過這支才能決定文章屬於哪個話題；這支只負責把標籤欄位填進
pipeline/topic_db.py 的 articles 表。
"""
from __future__ import annotations

import json

from ingestion.base import RawItem
from pipeline.llm_client import get_client, get_model
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_CONTENT_CHARS_FOR_TAGGING = 6000


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def tag_article(item: RawItem, client=None) -> dict:
    """對單篇文章抽取標籤，回傳 prompts/article_tagging.md 的完整解析結果。"""
    client = client or get_client()
    source_name = item.extra.get("source_name", item.subdomain_id)

    system, user = load_prompt_parts(
        "article_tagging",
        source_name=source_name,
        title=item.title,
        published_date=item.published_at.date().isoformat(),
        content=item.summary[:_CONTENT_CHARS_FOR_TAGGING],
    )

    response = client.chat.completions.create(
        model=get_model(),
        max_tokens=3000,
        temperature=0.2,
        reasoning_effort="low",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    log_call("article_tagging", system, user, raw_text, parsed)
    return parsed
