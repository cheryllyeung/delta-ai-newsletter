"""每期摘要頁頂部的 TLDR（趨勢／重點／觀察），genomics 2026-09-01 加。

輸入是本期已生成文章的標題與卡片摘要（不是原始全文，TLDR 是對「本期
內容」的總結，不是重新讀來源），一期一次呼叫。失敗不擋出刊，摘要頁
沒有 TLDR 區塊照樣能看。
"""
from __future__ import annotations

import json
import sqlite3

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text, strict=False)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1], strict=False)
        raise


def write_issue_tldr(conn: sqlite3.Connection, issue_id: int, client=None) -> dict | None:
    """為一期生成 TLDR 並存進 issues.tldr_json，回傳解析結果（失敗回 None）。"""
    rows = conn.execute(
        "SELECT generated_json FROM generated_topics WHERE issue_id = ? ORDER BY id", (issue_id,)
    ).fetchall()
    if not rows:
        return None

    lines = []
    for r in rows:
        g = json.loads(r["generated_json"] if isinstance(r, sqlite3.Row) else r[0])
        summary = (g.get("card_summary") or {}).get("text", "")
        lines.append(f"- {g.get('chosen_headline', '')}：{summary}")

    client = client or get_client()
    system, user = load_prompt_parts(
        "issue_tldr", article_count=str(len(lines)), articles_text="\n".join(lines)
    )
    try:
        response = create_chat_completion(
            client,
            model=get_model(),
            max_tokens=1200,
            temperature=0.3,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **reasoning_effort_kwargs(),
        )
        raw = response.choices[0].message.content
        parsed = _parse_json_object(raw)
        log_call("issue_tldr", system, user, raw, parsed)
    except Exception as exc:  # noqa: BLE001 -- TLDR 失敗不能擋出刊
        print(f"[issue_tldr] 生成失敗，這期沒有 TLDR：{exc}")
        return None

    conn.execute(
        "UPDATE issues SET tldr_json = ? WHERE id = ?",
        (json.dumps(parsed, ensure_ascii=False), issue_id),
    )
    conn.commit()
    return parsed
