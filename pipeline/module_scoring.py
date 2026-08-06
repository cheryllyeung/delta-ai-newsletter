"""階段三：對每個候選話題做 18 模組打分 + 內容型態判斷（prompts/module_scoring.md）。

打分的輸入是話題底下所有文章的標籤與摘要（見 pipeline/article_tagging.py），
不是原始全文，跟 PRD §4 階段三「熱門度評分」用的是報導家數這種結構化訊號、
不是全文本身，是同一個精神：選題階段看的是「這個話題大概在講什麼」，
全文留到階段四寫作時才真正派上用場。
"""
from __future__ import annotations

import json
import sqlite3

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_REQUIRED_MODULE_IDS = [
    "legal_compliance",
    "finance_audit",
    "hr",
    "marketing_brand",
    "it_security",
    "ops_logistics",
    "strategy_investment",
    "rd_engineering",
    "knowledge_management",
    "ehs",
    "energy_power",
    "building_automation",
    "ev_automotive",
    "network_infra",
    "manufacturing",
    "consumer_products",
    "software_platform",
    "sustainability",
]


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise


def build_modules_list_text(modules_config: dict) -> str:
    lines = []
    for group in ("functional", "domain"):
        for module in modules_config[group]:
            lines.append(f"- {module['id']}: {module['name']}")
    return "\n".join(lines)


def _build_article_summaries_text(article_rows: list[sqlite3.Row]) -> str:
    lines = []
    for row in article_rows:
        tags = (
            json.loads(row["tech_tags_json"] or "[]")
            + json.loads(row["entity_tags_json"] or "[]")
            + json.loads(row["scenario_tags_json"] or "[]")
            + json.loads(row["industry_tags_json"] or "[]")
        )
        lines.append(f"- [{row['source_name']}] {row['one_line_summary']}（標籤：{'、'.join(tags)}）")
    return "\n".join(lines)


def _validate_module_scores(module_scores: dict) -> None:
    missing = [mid for mid in _REQUIRED_MODULE_IDS if mid not in module_scores]
    if missing:
        raise ValueError(f"module_scores 缺少模組：{missing}")


def score_topic(
    topic_row: sqlite3.Row,
    article_rows: list[sqlite3.Row],
    modules_config: dict,
    client=None,
) -> dict:
    """對單一話題打 18 模組分數，回傳 {"module_scores": {...}, "content_type": str}。"""
    client = client or get_client()

    system, user = load_prompt_parts(
        "module_scoring",
        topic_title=topic_row["representative_title"],
        article_summaries=_build_article_summaries_text(article_rows),
        modules_list=build_modules_list_text(modules_config),
    )

    response = create_chat_completion(
        client,
        model=get_model(),
        max_tokens=5000,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **reasoning_effort_kwargs(),
    )
    raw_text = response.choices[0].message.content
    parsed = _parse_json_object(raw_text)
    _validate_module_scores(parsed["module_scores"])
    log_call("module_scoring", system, user, raw_text, parsed)
    return parsed
