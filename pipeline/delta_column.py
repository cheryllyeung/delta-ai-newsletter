"""台達專欄：週報的固定欄目，每個事業領域一格，回顧當週最相關的一件事。

2026-08-26 加，是週報的賣點：18 模組打分本來就在幫每個話題按事業群評分，
這裡把那份資料變成版面。九個欄位＝8 個 domain 模組＋研發工程（開發事業）。

兩種狀態（實測四週裡樓宇自動化有三週、消費性產品有兩週沒有 ≥6 分的話題，
「每群每週都有料」不成立；使用者決定沒料的格子直接不出現，不顯示
「本週無重點動態」這類空格文字，所以格數每週浮動）：

- featured：有過選題門檻（>=6）的話題，配一段 LLM 寫的台達視角短評
- minor：只有 4-6 分的次要動態，一樣配短評但標註「次要動態」

另外每期週報有一個主題大標題（write_weekly_headline()），從當週專欄的
主打話題提煉，風格照使用者給的歷史專題範本（見 prompts/weekly_headline.md）。

短評刻意一格一格各寫各的，不把九件事湊成一篇長文。「把不相干素材硬湊
成一篇」的教訓見 config/topics.yaml 的 sources_for_writing 註解。
"""
from __future__ import annotations

import json
import sqlite3

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

# genomics-prototype：專欄欄位改成四個主題模組（週報 v1 未啟用，先對齊
# config 免得跑起來炸 KeyError）。
COLUMN_MODULE_IDS = [
    "corp_market",
    "tech_breakthrough",
    "policy_regulation",
    "clinical_application",
]

# minor 狀態的下限。featured 的下限直接沿用 selection.weekly 的選題門檻，
# 專欄跟正刊對「夠格」的定義一致。
_MINOR_MIN_SCORE = 4.0

_SUMMARY_CHARS = 600


def _topic_summary(conn: sqlite3.Connection, topic_id: int) -> str:
    rows = conn.execute(
        """SELECT title, one_line_summary, content FROM articles
           WHERE topic_id = ? AND discarded_at IS NULL ORDER BY published_at""",
        (topic_id,),
    ).fetchall()
    parts = []
    for r in rows:
        if r["one_line_summary"]:
            parts.append(f"- {r['one_line_summary']}")
        elif r["content"]:
            parts.append(f"- {r['title']}：{r['content'][:_SUMMARY_CHARS]}")
        else:
            parts.append(f"- {r['title']}（只有標題）")
    return "\n".join(parts)[:2000]


def _write_comment(client, module: dict, topic_title: str, summary: str, reason: str) -> str | None:
    """一格短評一次呼叫，失敗回 None（該格降級成只有標題沒有短評，不擋出刊）。"""
    system, user = load_prompt_parts(
        "delta_column",
        module_name=module["name"],
        module_desc=module.get("desc", ""),
        topic_title=topic_title,
        topic_summary=summary,
        score_reason=reason,
    )
    try:
        response = create_chat_completion(
            client,
            model=get_model(),
            max_tokens=500,
            temperature=0.3,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **reasoning_effort_kwargs(),
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1], strict=False)
        log_call("delta_column", system, user, raw, parsed)
        return str(parsed["comment"])
    except Exception as exc:  # noqa: BLE001 -- 專欄短評失敗不能擋週報出刊
        print(f"[delta_column]   {module['name']} 短評生成失敗，該格只列標題：{exc}")
        return None


def build_delta_column(conn: sqlite3.Connection, config: dict, date_range: tuple[str, str]) -> list[dict]:
    """回傳九格專欄資料（照 COLUMN_MODULE_IDS 順序），直接序列化存進
    issues.column_json。每格：module_id / module_name / state，featured 與
    minor 另有 topic_id / topic_title / score / comment / generated_id（連結
    用，可能為 None）。
    """
    modules_by_id = {
        m["id"]: m for group in ("functional", "domain") for m in config["modules"][group]
    }
    featured_min = config["selection"]["weekly"]["min_module_score_to_select"]

    rows = conn.execute(
        """SELECT t.id, t.representative_title, t.module_scores_json FROM topics t
           WHERE t.module_scores_json IS NOT NULL AND EXISTS (
             SELECT 1 FROM articles a WHERE a.topic_id = t.id
             AND date(a.published_at) BETWEEN ? AND ?)""",
        date_range,
    ).fetchall()

    # 一個話題只掛一個欄位（它分數最高的那格），避免同一件事霸佔多格。
    best_per_group: dict[str, tuple[float, sqlite3.Row, str]] = {}
    for row in rows:
        scores = json.loads(row["module_scores_json"])
        in_column = {gid: scores[gid] for gid in COLUMN_MODULE_IDS if gid in scores}
        if not in_column:
            continue
        home_group = max(in_column, key=lambda gid: in_column[gid]["score"])
        entry = in_column[home_group]
        current = best_per_group.get(home_group)
        if current is None or entry["score"] > current[0]:
            best_per_group[home_group] = (entry["score"], row, entry.get("reason", ""))

    client = get_client()
    column: list[dict] = []
    for gid in COLUMN_MODULE_IDS:
        module = modules_by_id[gid]
        best = best_per_group.get(gid)
        if best is None or best[0] < _MINOR_MIN_SCORE:
            continue  # 沒料的格子直接不出現（2026-08-26 使用者定的）
        score, topic_row, reason = best
        state = "featured" if score >= featured_min else "minor"
        comment = _write_comment(
            client, module, topic_row["representative_title"],
            _topic_summary(conn, topic_row["id"]), reason,
        )
        generated = conn.execute(
            """SELECT id, issue_id FROM generated_topics WHERE topic_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (topic_row["id"],),
        ).fetchone()
        column.append(
            {
                "module_id": gid,
                "module_name": module["name"],
                "state": state,
                "topic_id": topic_row["id"],
                "topic_title": topic_row["representative_title"],
                "score": score,
                "comment": comment,
                "generated_id": generated["id"] if generated else None,
                "generated_issue_id": generated["issue_id"] if generated else None,
            }
        )
    return column


def write_weekly_headline(
    client, column: list[dict], date_range: tuple[str, str]
) -> str | None:
    """從當週專欄的主打話題提煉這期週報的主題大標題。失敗回 None（頁面
    退回顯示預設刊名，不擋出刊）。"""
    featured = [c for c in column if c.get("state") == "featured"] or column
    if not featured:
        return None
    featured_list = "\n".join(
        f"- {c['module_name']}：{c['topic_title']}" for c in featured
    )
    system, user = load_prompt_parts(
        "weekly_headline",
        week_start=date_range[0],
        week_end=date_range[1],
        featured_list=featured_list,
    )
    try:
        response = create_chat_completion(
            client,
            model=get_model(),
            max_tokens=300,
            temperature=0.4,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **reasoning_effort_kwargs(),
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1], strict=False)
        log_call("weekly_headline", system, user, raw, parsed)
        return str(parsed["headline"])
    except Exception as exc:  # noqa: BLE001 -- 標題失敗不擋出刊
        print(f"[delta_column]   週報大標題生成失敗：{exc}")
        return None
