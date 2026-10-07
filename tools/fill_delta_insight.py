"""補上缺少「對行動基因的意義」的那幾則（2026-10-07 加）。

為什麼需要：生成端原本只要求洞見型（insight）寫 delta_insight，警示型與
快訊型都寫 null，所以第 14 期九則有三則沒有。使用者指出這一段正是讀者要的，
每則都該有。生成 prompt 已經改成每種型態都要寫，但已經產出的期數要靠這支補。

只補 delta_insight 欄位，不動標題、摘要與內文，所以不必重跑整篇生成。

用法：
    python -m tools.fill_delta_insight --issue-id 14
    python -m tools.fill_delta_insight --issue-id 14 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts
from pipeline.topic_db import get_articles_for_topic, get_connection

ROOT = Path(__file__).resolve().parent.parent
MAX_SOURCE_CHARS = 6000


def _sources_text(conn, topic_id: int) -> str:
    parts = []
    for row in get_articles_for_topic(conn, topic_id):
        head = f"【{row['source_name']}】{row['title']}"
        parts.append(head + chr(10) + (row["content"] or "").strip())
    return (chr(10) * 2).join(parts)[:MAX_SOURCE_CHARS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))
    if args.issue_id:
        issue_id = args.issue_id
    else:
        issue_id = conn.execute("SELECT max(id) FROM issues").fetchone()[0]

    rows = conn.execute(
        "SELECT id, topic_id, generated_json FROM generated_topics WHERE issue_id = ? ORDER BY id",
        (issue_id,),
    ).fetchall()
    client = get_client()
    filled = skipped = failed = 0

    for r in rows:
        g = json.loads(r["generated_json"])
        headline = g.get("chosen_headline") or ""
        if g.get("delta_insight"):
            skipped += 1
            print(f"  已有　{headline[:40]}")
            continue

        system, user = load_prompt_parts(
            "delta_insight_fill",
            headline=headline,
            summary=(g.get("card_summary") or {}).get("text", ""),
            sources_text=_sources_text(conn, r["topic_id"]),
        )
        try:
            response = create_chat_completion(
                client,
                model=get_model(),
                max_tokens=800,
                temperature=0.4,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **reasoning_effort_kwargs(),
            )
            raw = response.choices[0].message.content
        except Exception as exc:  # noqa: BLE001 -- 單則失敗不中斷整批
            failed += 1
            print(f"  呼叫失敗　{headline[:36]}：{str(exc)[:50]}")
            continue
        try:
            from pipeline.article_tagging import _parse_json_object

            parsed = _parse_json_object(raw)
        except Exception as exc:  # noqa: BLE001
            log_call("delta_insight_fill", system, user, raw, None)
            failed += 1
            print(f"  解析失敗　{headline[:36]}：{str(exc)[:50]}")
            continue
        log_call("delta_insight_fill", system, user, raw, parsed)

        paragraphs = parsed.get("paragraphs") or []
        if not paragraphs:
            failed += 1
            print(f"  內容為空　{headline[:36]}")
            continue
        filled += 1
        print(f"  補上　{headline[:34]}")
        print(f"       {parsed.get('heading', '')}")
        print(f"       {paragraphs[0][:100]}")
        if not args.dry_run:
            g["delta_insight"] = {
                "heading": parsed.get("heading", "對行動基因的意義"),
                "paragraphs": paragraphs,
            }
            conn.execute(
                "UPDATE generated_topics SET generated_json = ? WHERE id = ?",
                (json.dumps(g, ensure_ascii=False), r["id"]),
            )

    if not args.dry_run:
        conn.commit()
    print(
        f"[fill_delta_insight] 第 {issue_id} 期：補上 {filled} 則，原本就有 {skipped} 則，失敗 {failed} 則"
        + ("（dry-run，沒有寫回）" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
