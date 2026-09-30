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


def dimension_by_topic(tldr: dict | None) -> dict[int, str]:
    """{topic_id: 面向}。2026-09-30 起 items 只做分類，不再自己寫導讀文字。

    舊期數的 items 是另寫的一句話、沒有 topic_id，這時回空 dict，呼叫端
    自己退回舊的顯示方式。
    """
    if not tldr:
        return {}
    out: dict[int, str] = {}
    for e in tldr.get("items") or []:
        if isinstance(e, dict) and isinstance(e.get("topic_id"), int) and e.get("dimension"):
            out[e["topic_id"]] = e["dimension"]
    return out


def dimension_groups(entries: list[dict]) -> list[dict] | None:
    """把版面上的報導按面向分組，導讀直接顯示它們的原標題。

    entries: [{"num": int, "dimension": str, "title": str}]，num 是版面上
    印的序號。2026-09-30 改版的重點：導讀與報導共用同一份標題字串，上下
    絕對一致（之前導讀另外寫一句，讀者以為是兩則不同的報導）。
    """
    by_dim: dict[str, list[str]] = {}
    for e in entries:
        if e.get("dimension") and e.get("title"):
            by_dim.setdefault(e["dimension"], []).append(f"{e['num']:02d}　{e['title']}")
    if not by_dim:
        return None
    order = _DIMENSION_ORDER + [d for d in by_dim if d not in _DIMENSION_ORDER]
    return [{"label": d, "entries": by_dim[d]} for d in order if d in by_dim]


def tldr_display_groups(tldr: dict | None) -> list[dict] | None:
    """把 tldr_json 整理成版面用的分組 [{"label": str, "entries": [str]}]。

    2026-09-21 改版：導讀從「趨勢／重點／觀察」三類改成按面向（市場／
    技術／臨床／法規）分大區塊，三類界線模糊（同一條放哪類都說得通），
    面向分類比較直覺。

    2026-09-30 起的期數走 dimension_groups（導讀直接列原標題），這支只
    負責舊期數的退路：

    - 2026-09-21 到 09-29：items 帶 dimension 與自己的 text
    - 2026-09-18 到 09-20：trends/highlights/observations 三列帶 dimension
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

    if all(isinstance(e, dict) and e.get("dimension") and e.get("text") for e in entries):
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
        "SELECT topic_id, generated_json FROM generated_topics WHERE issue_id = ? ORDER BY id",
        (issue_id,),
    ).fetchall()
    if not rows:
        return None

    # 編號要跟版面上的序號一致：模型只回「第幾則屬於哪個面向」，導讀顯示
    # 的文字由版面直接取原標題（2026-09-30 改版，見 tldr_display_groups）。
    lines = []
    for i, r in enumerate(rows, 1):
        g = json.loads(r["generated_json"] if isinstance(r, sqlite3.Row) else r[0])
        summary = (g.get("card_summary") or {}).get("text", "")
        lines.append(f"{i}. {g.get('chosen_headline', '')}：{summary}")

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

    # 分類要覆蓋每一則，不然導讀會漏掉報導。序號超出範圍或重複的丟掉，
    # 沒分到的補「市場」（四個面向裡最不會誤導的落點），並印出來讓人知道。
    # 面向存 topic_id 而不只是序號：EDM 會把主要報導排到前面，網頁又是
    # 另一個順序，序號在不同版面對不上，topic_id 到哪都認得。
    topic_ids = [r["topic_id"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]
    items, seen = [], set()
    for e in parsed.get("items") or []:
        if not isinstance(e, dict):
            continue
        idx, dim = e.get("index"), e.get("dimension")
        if isinstance(idx, int) and 1 <= idx <= len(rows) and dim in _DIMENSION_ORDER and idx not in seen:
            seen.add(idx)
            items.append({"index": idx, "topic_id": topic_ids[idx - 1], "dimension": dim})
    missing = [i for i in range(1, len(rows) + 1) if i not in seen]
    if missing:
        print(f"[issue_tldr] 這幾則沒分到面向，補為「市場」：{missing}")
        items.extend(
            {"index": i, "topic_id": topic_ids[i - 1], "dimension": "市場"} for i in missing
        )
    parsed["items"] = sorted(items, key=lambda x: x["index"])

    # 主編觀察存在同一個 tldr_json 裡、而且是在這支之後才寫的，整包覆寫
    # 會把它清掉（2026-09-30 單獨重跑這支時實際清掉過一次）。先把原有的
    # 內容讀回來合併。
    prev = conn.execute("SELECT tldr_json FROM issues WHERE id = ?", (issue_id,)).fetchone()
    merged = {}
    if prev and prev[0]:
        try:
            merged = json.loads(prev[0])
        except json.JSONDecodeError:
            merged = {}
    merged.update(parsed)
    conn.execute(
        "UPDATE issues SET tldr_json = ? WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False), issue_id),
    )
    conn.commit()
    return merged or parsed
