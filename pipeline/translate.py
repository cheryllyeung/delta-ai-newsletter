"""文章內文的多語版本：繁中原文 → 簡中、英文。

兩種語言的成本差很多，所以走不同路徑：

* **簡中**：opencc 的詞彙級轉換（t2s），純字串處理、即時、不花 LLM 額度，
  所以不需要存起來，每次渲染時轉即可。跟 pipeline/text_normalize.py 的
  逐字元 s2t 是反方向、不同用途：那支是修 LLM 夾雜簡體字的保險絲，會刻意
  避開詞彙替換；這支是給讀者看的正式簡中版，用詞彙級轉換讀起來才自然。
* **英文**：要真的翻譯，得打 LLM。一篇文章的 JSON 結構有標題、副標、多個
  段落、數據標籤等等，一次翻完比逐欄位翻省很多呼叫，所以整個物件一起送。
  翻完存進 generated_topics.translations_json，之後直接讀快取。
"""
from __future__ import annotations

import json
import sqlite3

import opencc

from pipeline.llm_client import create_chat_completion, get_client, get_writing_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts

SUPPORTED = ("zh-Hant", "zh-Hans", "en")

# 詞彙級轉換（不是逐字元），「軟體」會變成「软件」而不是「软體」。
_to_simplified = opencc.OpenCC("tw2sp")

# 這些欄位是機器讀的識別字（content_type、模組 id 之類），翻譯會壞掉下游邏輯。
_SKIP_KEYS = {"primary_tag_id", "content_type", "id", "url", "metric_value"}


def to_simplified(value):
    """遞迴把 dict/list/str 轉成簡體。"""
    if isinstance(value, str):
        return _to_simplified.convert(value)
    if isinstance(value, list):
        return [to_simplified(v) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in _SKIP_KEYS else to_simplified(v)) for k, v in value.items()}
    return value


def translate_to_english(article: dict, client=None) -> dict:
    """把整篇文章 JSON 一次翻成英文，保持結構與 key 不變。"""
    client = client or get_client()
    system, user = load_prompt_parts(
        "translate_article",
        article_json=json.dumps(article, ensure_ascii=False, indent=2),
    )
    response = create_chat_completion(
        client,
        model=get_writing_model(),
        max_tokens=4000,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **reasoning_effort_kwargs(),
    )
    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            log_call("translate_article", system, user, raw, None)
            raise
        parsed = json.loads(raw[start : end + 1])
    log_call("translate_article", system, user, raw, parsed)
    return parsed


def pretranslate_issue(conn: sqlite3.Connection, issue_id: int) -> tuple[int, int]:
    """出刊後先把整期的英文版翻好存起來，讓讀者點開就是秒開。

    不做的話第一個點英文版的人要等一分鐘（實測 65 秒／篇）。排程在中午跑，
    多花的兩三分鐘不影響任何人。翻不出來就跳過，之後有人點開時會再試一次，
    絕對不能讓翻譯失敗影響到已經出好的這一期。

    回傳 (成功數, 失敗數)。
    """
    rows = conn.execute(
        "SELECT id, generated_json FROM generated_topics WHERE issue_id = ?", (issue_id,)
    ).fetchall()
    ok = failed = 0
    for row in rows:
        article = json.loads(row["generated_json"])
        before = conn.execute(
            "SELECT translations_json FROM generated_topics WHERE id = ?", (row["id"],)
        ).fetchone()["translations_json"]
        get_article_in(conn, row["id"], article, "en")
        after = conn.execute(
            "SELECT translations_json FROM generated_topics WHERE id = ?", (row["id"],)
        ).fetchone()["translations_json"]
        if after and after != before:
            ok += 1
        else:
            failed += 1
    return ok, failed


def get_article_in(
    conn: sqlite3.Connection, generated_id: int, article: dict, lang: str
) -> dict:
    """回傳指定語言的文章內容。

    zh-Hant 直接回原文；zh-Hans 即時轉換；en 先看快取，沒有才翻並存起來。
    翻譯失敗時回傳原文而不是拋例外：語言切換是加值功能，不該讓整頁掛掉。
    """
    if lang == "zh-Hant" or lang not in SUPPORTED:
        return article
    if lang == "zh-Hans":
        return to_simplified(article)

    row = conn.execute(
        "SELECT translations_json FROM generated_topics WHERE id = ?", (generated_id,)
    ).fetchone()
    cached = json.loads(row["translations_json"]) if row and row["translations_json"] else {}
    if lang in cached:
        return cached[lang]

    try:
        translated = translate_to_english(article)
    except Exception as exc:  # noqa: BLE001 -- 翻不出來就退回原文，不要讓頁面掛掉
        print(f"[translate] 英文翻譯失敗，改用原文：{exc}")
        return article

    cached[lang] = translated
    conn.execute(
        "UPDATE generated_topics SET translations_json = ? WHERE id = ?",
        (json.dumps(cached, ensure_ascii=False), generated_id),
    )
    conn.commit()
    return translated
