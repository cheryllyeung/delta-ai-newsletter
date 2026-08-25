"""階段二：對每篇文章抽取多維關鍵詞標籤與案例標記（prompts/article_tagging.md）。

跟話題聚類（pipeline/topic_clustering.py）完全脫鉤：聚類只看向量相似度，
不需要跑過這支才能決定文章屬於哪個話題；這支只負責把標籤欄位填進
pipeline/topic_db.py 的 articles 表。
"""
from __future__ import annotations

import json
import re

from ingestion.base import RawItem
from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_CONTENT_CHARS_FOR_TAGGING = 6000


# gateway 的模型輸出中文字串值時，偶爾會漏掉「開頭」的引號（結尾引號還在），
# 長這樣：
#     "ai_relevance_reason": 主題為 Google Gemini 用戶數。",
# 2026-08-14 實測 108 篇裡 82 篇壞在這裡，壞法非常固定，可以安全修：
# 值的第一個字元不是任何合法 JSON 起始字元（引號、括號、數字、負號、
# true/false/null 的開頭）、而且行尾有結尾引號時，補上開頭引號。
# 兩個條件都不成立的行不動，修不好就讓原本的解析錯誤照常拋出。
# 排除清單裡有 \s 是為了擋回溯：沒有它的話，正常的 `: "watch"` 這種行會在
# 引號被 lookahead 擋掉後回溯少吃一個空白，讓空白字元通過檢查，然後把引號
# 插在錯的位置。把空白也列為「不合法的值開頭」，回溯的每個位置都過不了。
_MISSING_OPEN_QUOTE = re.compile(r'^(\s*"[\w_]+"\s*:\s*)(?!["\[{tfn\d\s-])(.+?)(",?\s*)$', re.M)


def _repair_missing_open_quote(text: str) -> str:
    return _MISSING_OPEN_QUOTE.sub(r'\1"\2\3', text)


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start == -1 or end == -1:
            raise
        snippet = raw_text[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return json.loads(_repair_missing_open_quote(snippet))


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

    response = create_chat_completion(
        client,
        model=get_model(),
        max_tokens=3000,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **reasoning_effort_kwargs(),
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    log_call("article_tagging", system, user, raw_text, parsed)
    return parsed
