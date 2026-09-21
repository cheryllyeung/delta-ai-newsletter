"""智庫觀察：日報的智庫來源專屬欄目，2026-09-21 加。

台灣智庫（DSET、中經院，config 的 thinktank_watch.source_ids）的文章是
政策與供應鏈研究，性質跟產業新聞不同：價值在觀點不在時效，放進正刊跟
廠商動態排排站容易被淹掉。這裡比照台達專欄（delta_column.py）做成獨立
欄目：每篇智庫文章一格，短評講它的論點，再附一段對台達的啟示。

生成紀律跟正刊同一套：只根據素材寫、不編數字、啟示維持推測語氣（見
prompts/thinktank_watch.md）。存 issues.thinktank_json，日報出刊時生成，
失敗不擋出刊（該格只剩標題與連結，或整個欄目缺席）。
"""
from __future__ import annotations

import json
import sqlite3

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

_CONTENT_CHARS = 4000


def _top_score(module_scores_json: str | None) -> float:
    if not module_scores_json:
        return 0.0
    scores = json.loads(module_scores_json)
    return max((v.get("score", 0.0) for v in scores.values()), default=0.0)


def _write_cell(client, source_name: str, title: str, body: str) -> dict | None:
    """一格一次呼叫，回 {"comment": str, "delta_insight": str}，失敗回 None
    （該格降級成只有標題與連結，不擋出刊）。"""
    system, user = load_prompt_parts(
        "thinktank_watch",
        source_name=source_name,
        article_title=title,
        article_body=body[:_CONTENT_CHARS],
    )
    try:
        response = create_chat_completion(
            client,
            model=get_model(),
            max_tokens=700,
            temperature=0.3,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **reasoning_effort_kwargs(),
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw[raw.find("{"): raw.rfind("}") + 1], strict=False)
        log_call("thinktank_watch", system, user, raw, parsed)
        return {
            "comment": str(parsed["comment"]),
            "delta_insight": str(parsed["delta_insight"]),
        }
    except Exception as exc:  # noqa: BLE001 -- 欄目短評失敗不能擋日報出刊
        print(f"[thinktank_watch]   「{title[:30]}」短評生成失敗，該格只列標題：{exc}")
        return None


def build_thinktank_watch(
    conn: sqlite3.Connection,
    config: dict,
    date_range: tuple[str, str],
    exclude_topic_ids: set[int] | None = None,
) -> list[dict]:
    """回傳智庫觀察的格子清單，直接序列化存進 issues.thinktank_json。

    候選是窗口內智庫來源、通過收錄判定的文章，一篇文章一格（智庫文章幾乎
    都是單獨成題），照所屬話題的最高模組分排序取前 max_cells 格。已經被
    這期正刊選走的話題不再進欄目，避免同一件事出現兩次。
    """
    cfg = config.get("thinktank_watch") or {}
    source_ids = cfg.get("source_ids") or []
    if not source_ids:
        return []
    max_cells = int(cfg.get("max_cells", 3))
    min_score = float(cfg.get("min_module_score", 4.0))
    exclude_topic_ids = exclude_topic_ids or set()

    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""SELECT a.id, a.source_name, a.title, a.url, a.one_line_summary,
                   a.content, a.topic_id, t.module_scores_json
            FROM articles a JOIN topics t ON t.id = a.topic_id
            WHERE a.source_id IN ({placeholders})
              AND a.discarded_at IS NULL
              AND a.gate_status = 'included'
              AND date(a.published_at) BETWEEN ? AND ?
            ORDER BY a.published_at DESC""",
        (*source_ids, *date_range),
    ).fetchall()

    candidates = []
    for r in rows:
        if r["topic_id"] in exclude_topic_ids:
            continue
        score = _top_score(r["module_scores_json"])
        if score < min_score:
            continue
        candidates.append((score, r))
    candidates.sort(key=lambda pair: pair[0], reverse=True)

    client = get_client()
    cells: list[dict] = []
    for score, r in candidates[:max_cells]:
        body = r["one_line_summary"] or ""
        if r["content"]:
            body = f"{body}\n{r['content']}" if body else r["content"]
        written = _write_cell(client, r["source_name"], r["title"], body)
        cells.append(
            {
                "source_name": r["source_name"],
                "article_title": r["title"],
                "article_url": r["url"],
                "topic_id": r["topic_id"],
                "score": round(score, 2),
                "comment": written["comment"] if written else None,
                "delta_insight": written["delta_insight"] if written else None,
            }
        )
    return cells
