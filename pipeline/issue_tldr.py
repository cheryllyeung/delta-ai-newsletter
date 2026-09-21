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


# 版面上面向區塊的固定順序（2026-09-21 改版，使用者主管定的：市場、
# 技術、臨床，法規當天有新聞才出現）。
_DIMENSION_ORDER = ["市場", "技術", "臨床", "法規"]


def tldr_display_groups(tldr: dict | None) -> list[dict] | None:
    """把 tldr_json 整理成版面用的分組 [{"label": str, "entries": [str]}]。

    2026-09-21 改版：導讀從「趨勢／重點／觀察」三類改成按面向（市場／
    技術／臨床／法規）分大區塊。主管反映三類界線模糊（同一條放哪類都
    說得通），面向分類比較直覺。三種存檔格式都要吃：
    - 新格式：items 一層，每條帶 dimension
    - 2026-09-18 到 09-20：trends/highlights/observations 三列，每條帶
      dimension，合併後照樣能按面向分組
    - 9/18 前：三列純字串沒有面向，只能維持原本的三類標籤
    """
    if not tldr:
        return None
    entries = tldr.get("items")
    if entries is None:
        entries = []
        for key in ("trends", "highlights", "observations"):
            entries.extend(tldr.get(key) or [])
    if not entries:
        return None

    if all(isinstance(e, dict) and e.get("dimension") for e in entries):
        by_dim: dict[str, list[str]] = {}
        for e in entries:
            by_dim.setdefault(e["dimension"], []).append(e.get("text", ""))
        order = _DIMENSION_ORDER + [d for d in by_dim if d not in _DIMENSION_ORDER]
        return [{"label": d, "entries": by_dim[d]} for d in order if d in by_dim]

    # 舊格式（純字串）退回三類標籤
    groups = []
    for key, label in (("trends", "趨勢"), ("highlights", "重點"), ("observations", "觀察")):
        items = tldr.get(key) or []
        texts = [i.get("text", "") if isinstance(i, dict) else i for i in items]
        if texts:
            groups.append({"label": label, "entries": texts})
    return groups or None


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
            max_tokens=2500,  # 2026-09-18 加 headline 與 editorial，1200 會截斷
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
