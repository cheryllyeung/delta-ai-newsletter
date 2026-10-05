"""摘要與標題重複時重寫摘要（2026-10-05 加）。

使用者反映「下方文章標題跟摘要重複性太高」，實測第 11 期八則平均重疊 54%，
最高兩則 84% 與 78%，正好就是他挑出來的那兩則。這支負責把超過門檻的重寫掉：
重算一次、再量重疊，沒降就重試一次，仍然沒降就保留原本的，不會越改越糟。

出刊流程（scripts/compose_topic_issue.py）每期自動跑一次，也可以用
tools/rewrite_card_summaries.py 針對某一期單獨跑。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts
from pipeline.summary_overlap import THRESHOLD, overlap
from pipeline.topic_db import get_articles_for_topic

MAX_SOURCE_CHARS = 6000


def _parse(raw: str) -> dict:
    """解析模型回的 JSON。重用 article_tagging 的修補：模型常漏掉字串值開頭
    那個引號（實測兩則就是這樣失敗），那支已經有對應的修補規則。"""
    from pipeline.article_tagging import _parse_json_object

    return _parse_json_object(raw)


def _sources_text(conn: sqlite3.Connection, topic_id: int) -> str:
    parts = []
    for row in get_articles_for_topic(conn, topic_id):
        head = f"【{row['source_name']}】{row['title']}"
        parts.append(head + "\n" + (row["content"] or "").strip())
    return "\n\n".join(parts)[:MAX_SOURCE_CHARS]


def rewrite_duplicative_summaries(
    conn: sqlite3.Connection,
    issue_id: int,
    *,
    threshold: float = THRESHOLD,
    client=None,
    dry_run: bool = False,
    verbose: bool = True,
) -> tuple[int, int]:
    """把這一期重複度過高的摘要重寫。回傳（改寫數, 保留原本數）。"""
    rows = conn.execute(
        "SELECT id, topic_id, generated_json FROM generated_topics WHERE issue_id = ? ORDER BY id",
        (issue_id,),
    ).fetchall()
    client = client or get_client()
    changed = kept = 0

    for r in rows:
        g = json.loads(r["generated_json"])
        headline = g.get("chosen_headline") or ""
        summary = (g.get("card_summary") or {}).get("text") or ""
        before = overlap(headline, summary)
        if before <= threshold:
            if verbose:
                print(f"  {before:4.0%} 保留　{headline[:34]}")
            continue

        best_text, best_score = summary, before
        for _ in (1, 2):
            system, user = load_prompt_parts(
                "card_summary_rewrite",
                headline=headline,
                subhead=g.get("chosen_subhead") or "",
                current_summary=best_text,
                sources_text=_sources_text(conn, r["topic_id"]),
            )
            try:
                response = create_chat_completion(
                    client,
                    model=get_model(),
                    max_tokens=600,
                    temperature=0.4,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    **reasoning_effort_kwargs(),
                )
                raw = response.choices[0].message.content
            except Exception as exc:  # noqa: BLE001 -- 單則失敗不中斷整批
                print(f"  呼叫失敗，保留原本：{str(exc)[:70]}")
                break
            # 解析失敗也要留原始回應（2026-10-05：原本 log 寫在 parse 之後，
            # 失敗那次什麼都沒留，查不到模型回了什麼）。
            try:
                parsed = _parse(raw)
            except Exception as exc:  # noqa: BLE001
                log_call("card_summary_rewrite", system, user, raw, None)
                print(f"  解析失敗（原始回應已留存）：{str(exc)[:60]}")
                continue
            log_call("card_summary_rewrite", system, user, raw, parsed)
            candidate = (parsed.get("text") or "").strip()
            if not candidate:
                continue
            score = overlap(headline, candidate)
            if score < best_score:
                best_text, best_score = candidate, score
            if best_score <= threshold:
                break

        if best_text != summary:
            changed += 1
            if verbose:
                print(f"  {before:4.0%} → {best_score:4.0%} 改寫　{headline[:30]}")
            if not dry_run:
                g.setdefault("card_summary", {})["text"] = best_text
                conn.execute(
                    "UPDATE generated_topics SET generated_json = ? WHERE id = ?",
                    (json.dumps(g, ensure_ascii=False), r["id"]),
                )
        else:
            kept += 1
            if verbose:
                print(f"  {before:4.0%} 改不下來，保留原本　{headline[:30]}")

    if not dry_run:
        conn.commit()
    return changed, kept
