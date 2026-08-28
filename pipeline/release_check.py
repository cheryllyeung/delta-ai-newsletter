"""發佈判定：判斷一篇文章是不是「模型或工具的官方發佈」（prompts/release_check.md）。

網頁的「模型與工具發佈」頁（/releases）靠這個標記選文章。獨立成一支
prompt 而不是併進 article_tagging，是因為標籤 prompt 一改新舊結果就不可比，
全池要重標一次才能對齊；發佈判定是 2026-08-28 後加的新維度，獨立一支
就只要補跑自己（tools/backfill_release_check.py），既有標籤完全不動。

判定結果由 topic_db.save_article_release() 寫進 articles 表的 is_release／
release_vendor／release_product／release_kind 欄位。
"""
from __future__ import annotations

from ingestion.base import RawItem
from pipeline.article_tagging import _parse_json_object
from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

# 判「是不是發佈」不需要全文，開頭就分得出來。比標籤的 6000 短是刻意的：
# 這一步是每篇都要跑的新增成本，能省就省。
_CONTENT_CHARS_FOR_CHECK = 4000


def check_release(item: RawItem) -> dict:
    client = get_client()
    source_name = item.extra.get("source_name", item.subdomain_id)

    system, user = load_prompt_parts(
        "release_check",
        source_name=source_name,
        title=item.title,
        published_date=item.published_at.date().isoformat(),
        content=item.summary[:_CONTENT_CHARS_FOR_CHECK],
    )

    response = create_chat_completion(
        client,
        model=get_model(),
        max_tokens=500,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **reasoning_effort_kwargs(),
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    log_call("release_check", system, user, raw_text, parsed)
    return parsed
