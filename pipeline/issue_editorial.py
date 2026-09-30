"""觀察專欄（2026-09-30 拆成獨立一支，同日改成四位分析師分面向）。

原本跟導讀（pipeline/issue_tldr.py）擠在同一次呼叫裡，產出是 2 到 3 段、
每段 80 到 150 字。EDM 改版後不再把全文放進信裡，讀者點原文看內容，
信件的價值集中在「選出哪幾則」與「專家怎麼看」，所以這一欄要更長、更有
觀點，也需要明確的人設（見 prompts/issue_editorial.md）。

同日再改成四位分析師分面向（市場／技術／法規／臨床），跟導讀用同一組
面向，整封信的骨架才統一。版面上四塊橫排：EDM 用 2x2 表格（Outlook 不
執行 JavaScript、也幾乎不吃媒體查詢，表格是唯一可靠的橫排方式），網頁
用自適應網格。

拆開的另一個理由是它跟導讀要的東西不一樣：導讀求準確扼要、溫度低；
這一欄要有判斷與語氣，溫度高一點。擠在一次呼叫裡兩邊都做不好。

失敗不擋出刊：信件少一個區塊，其餘照出。
"""
from __future__ import annotations

import json
import sqlite3

from pipeline.llm_client import create_chat_completion, get_client, get_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

# 四個面向，順序固定（跟導讀的分區同一組，整封信的骨架才統一）。
_DIMENSIONS = ["市場", "技術", "法規", "臨床"]
_MIN_SECTIONS = 2


def _parse_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text, strict=False)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1], strict=False)
        raise


def write_issue_editorial(
    conn: sqlite3.Connection, issue_id: int, newsletter_name: str, client=None
) -> list[dict] | None:
    """為一期生成四塊觀察，回傳 [{"dimension": str, "text": str}]（失敗回 None）。

    一次呼叫產出四塊而不是分四次：四位的說法之間不能互相矛盾，分開呼叫
    每位都看不到別人寫什麼，很容易講同一個論點或給出相反判斷。

    素材是本期每則的標題、摘要與涉及的廠商標籤（廠商標籤經過原文比對，
    見 pipeline/edm_tags.py），不是原始全文：這一欄要的是跨則的關聯，
    塞全文進去只會讓它退化成單篇評論。
    """
    from pipeline.edm_tags import tags_for_topic  # 延遲匯入避免循環

    rows = conn.execute(
        "SELECT topic_id, generated_json FROM generated_topics WHERE issue_id = ? ORDER BY id",
        (issue_id,),
    ).fetchall()
    if not rows:
        return None

    import yaml
    from pathlib import Path

    config = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "topics.yaml").read_text(encoding="utf-8")
    )

    lines = []
    for r in rows:
        g = json.loads(r["generated_json"])
        summary = (g.get("card_summary") or {}).get("text", "")
        tags = tags_for_topic(conn, r["topic_id"], config)
        tag_text = f"（涉及：{'、'.join(tags)}）" if tags else ""
        lines.append(f"- {g.get('chosen_headline', '')}{tag_text}：{summary}")

    client = client or get_client()
    system, user = load_prompt_parts(
        "issue_editorial",
        newsletter_name=newsletter_name,
        article_count=str(len(lines)),
        articles_text="\n".join(lines),
    )
    try:
        response = create_chat_completion(
            client,
            model=get_model(),
            max_tokens=2500,
            # 比導讀高一點：這一欄要有判斷與語氣，不是條列事實。
            temperature=0.45,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **reasoning_effort_kwargs(),
        )
        raw = response.choices[0].message.content
        parsed = _parse_json_object(raw)
        sections = parsed.get("sections") or []
        if not isinstance(sections, list) or len(sections) < _MIN_SECTIONS:
            raise ValueError(f"面向數不足：{sections}")
        clean = []
        for sec in sections:
            sec = sec or {}
            dim = sec.get("dimension")
            # 2026-09-30 起每塊分成 lead／body／question 三段（版面靠這個
            # 結構做層次）。舊格式只有 text，讀得到就當 body 用。
            lead = (sec.get("lead") or "").strip()
            body = (sec.get("body") or sec.get("text") or "").strip()
            question = (sec.get("question") or "").strip()
            if dim not in _DIMENSIONS or not body:
                continue
            # 一個面向只留一塊：模型偶爾會給兩塊「技術」、兩塊「臨床」，
            # 而漏掉市場與法規（2026-09-30 實測到），版面上會變成同一個
            # 小標出現兩次。保留第一塊，其餘丟掉。
            if any(c["dimension"] == dim for c in clean):
                continue
            clean.append({"dimension": dim, "lead": lead, "body": body, "question": question})
        # 追問克制：prompt 要求最多兩塊帶問題，模型偶爾會每塊都加。超過
        # 兩個就把後面的問題砍掉（照面向順序保留前兩個），不重擲整批。
        with_q = [c for c in clean if c["question"]]
        for extra in with_q[2:]:
            extra["question"] = ""
        if len(clean) < _MIN_SECTIONS:
            raise ValueError("通過驗證的面向不足")
        # 固定順序輸出，不照模型給的順序（避免每期版面順序跳動）。
        clean.sort(key=lambda s: _DIMENSIONS.index(s["dimension"]))
        log_call("issue_editorial", system, user, raw, parsed)
    except Exception as exc:  # noqa: BLE001 -- 觀察專欄失敗不能擋出刊
        print(f"[issue_editorial] 生成失敗，這期沒有觀察專欄：{exc}")
        return None

    # 存回 tldr_json：editorial_sections 是新格式（四塊），舊的 editorial
    # 欄位留著不動，讓改版前的期數照樣顯示得出來。
    row = conn.execute("SELECT tldr_json FROM issues WHERE id = ?", (issue_id,)).fetchone()
    tldr = json.loads(row["tldr_json"]) if row and row["tldr_json"] else {}
    tldr["editorial_sections"] = clean
    conn.execute(
        "UPDATE issues SET tldr_json = ? WHERE id = ?",
        (json.dumps(tldr, ensure_ascii=False), issue_id),
    )
    conn.commit()
    return clean
