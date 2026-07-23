"""Prompt 3：本期所有案例生成完之後，寫一次刊頭導言（呼應實際選出的案例）。"""
from __future__ import annotations

import json

import openai

from pipeline.llm_client import get_client, get_model
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def generate_intro(
    newsletter_name: str,
    selected_cases_summaries: list[str],
    dominant_tags: list[str],
    area_breakdown: dict[str, int],
    client: openai.OpenAI | None = None,
) -> dict:
    """依這期實際選出的內容組合動態生成導言，沒有預設主題可套用。

    dominant_tags：這期入選文章裡出現頻率最高的幾個 hashtag（呼叫端算好傳入）。
    area_breakdown：{"廠區現場": n, "後勤支援": n, "業務前台": n} 這種分佈。
    """
    client = client or get_client()

    summaries_text = "\n".join(f"- {s}" for s in selected_cases_summaries)
    tags_text = "、".join(dominant_tags) if dominant_tags else "（本期標籤分散，沒有明顯集中的主題）"
    breakdown_text = "、".join(f"{area}：{n}篇" for area, n in area_breakdown.items())

    system, user = load_prompt_parts(
        "intro",
        newsletter_name=newsletter_name,
        selected_cases_summaries=summaries_text,
        dominant_tags=tags_text,
        area_breakdown=breakdown_text,
    )

    response = client.chat.completions.create(
        model=get_model(),
        max_tokens=800,
        temperature=0.8,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    log_call("intro", system, user, raw_text, parsed)
    return parsed
