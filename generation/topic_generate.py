"""階段四：把入選話題（含 reranker 檢索出的 top-5 素材）生成成文章 JSON。

跟 generation/case_generate.py 是平行模組（那支服務 Delta Pulse 案例式
週報），JSON 解析容錯寫法沿用同一套慣例。
"""
from __future__ import annotations

import json
import random
import sqlite3

import openai

from pipeline.llm_client import create_chat_completion, get_client, get_writing_model, reasoning_effort_kwargs
from pipeline.llm_logging import log_call
from pipeline.prompt_loader import load_prompt_parts
from pipeline.text_normalize import fix_stray_simplified_in

_CONTENT_CHARS_PER_SOURCE = 1500

# JSON 解析失敗時重新生成的次數。
#
# 2026-08-11 實測：48 次寫作呼叫有 5 次解析失敗，而且不是隨機的，是兩種
# 固定的格式手滑：
#   * 4 次是 headline_candidates 陣列忘記用 ] 收尾就接著寫 chosen_headline，
#     全部斷在同一個位置（line 6 column 22）
#   * 1 次是字串值漏掉左引號（"text": n8n 結合...）
# 同一批呼叫裡 topic_self_check 43 次零失敗，它的 schema 簡單很多，所以問題
# 出在這支的巢狀結構，不是模型不會輸出 JSON。
#
# 一次解析失敗原本會讓整個話題被跳過（compose_topic_issue 接住例外後
# continue），連帶讓那一期少一篇——8/8 那期只出 1 篇、連配額下限 2 篇都
# 沒守住，就是這樣來的。temperature 是 0.7，重擲一次得到合法 JSON 的機率
# 很高，所以重試比放棄划算太多。
_JSON_RETRIES = 2

_CONTENT_TYPE_NAMES = {
    "insight": "洞見型",
    "practical": "實用型",
    "warning": "警示型",
    "flash": "快訊型",
}

# 開頭手法清單，跟 prompts/style_guide.md 的【第一段怎麼開頭】保持一致。
# 每篇話題是獨立的 LLM 呼叫，彼此看不到對方選了什麼手法，實測發現放給
# LLM 自己每篇挑的話，幾乎每篇都收斂成「情境假設句」（開頭都是「你...」），
# 「不要每篇都選同一種」這條指示在多篇獨立呼叫下沒辦法自己生效。改成由
# 呼叫端（scripts/compose_topic_issue.py）洗牌後輪流指派，同一期裡儘量
# 不重複；沒有明確指派時（例如單篇測試呼叫）就隨機挑一種，至少不會每次
# 都預設同一種。
OPENING_TECHNIQUES = [
    "情境假設句（把讀者放進一個具體處境裡）",
    "反差對比（兩個數字或兩個做法的落差）",
    "具體畫面比喻",
    "對讀者的犀利提問",
    "意外的數字或事實開場",
]


def _parse_json_object(raw_text: str) -> dict:
    # strict=False：LLM 常在字串值裡直接吐出沒跳脫的換行/tab 等控制字元，
    # 嚴格模式的 json.loads 會直接拋 JSONDecodeError（Invalid control character）。
    try:
        return json.loads(raw_text, strict=False)
    except json.JSONDecodeError:
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1], strict=False)
        raise


def build_sources_text(source_rows: list[sqlite3.Row]) -> str:
    """比照 sources_text 給寫作 prompt 用的格式，組出來源全文區塊。
    review/topic_selfcheck.py 的自檢也重用這支，確保審核跟寫作看到的
    是同一份來源文字，不會因為格式不同而誤判。"""
    blocks = []
    for row in source_rows:
        blocks.append(
            f"【來源：{row['source_name']}】\n"
            f"標題：{row['title']}\n"
            f"連結：{row['url']}\n"
            f"內文：{row['content'][:_CONTENT_CHARS_PER_SOURCE]}"
        )
    return "\n\n".join(blocks)


def generate_topic_article(
    newsletter_name: str,
    topic_title: str,
    content_type: str,
    source_rows: list[sqlite3.Row],
    revision_instructions: str | None = None,
    client: openai.OpenAI | None = None,
    opening_technique: str | None = None,
) -> dict:
    """依入選話題與 reranker 檢索出的來源文章清單生成文章。

    source_rows 是 pipeline.topic_db 的 articles 列，經
    pipeline.retrieval.retrieve_sources_for_topic() 撈出的 top-5，順序即為
    reranker 排序（相關度由高到低）。
    """
    client = client or get_client()
    opening_technique = opening_technique or random.choice(OPENING_TECHNIQUES)

    revision_note = ""
    if revision_instructions:
        revision_note = (
            "\n【上一版審查未通過，請針對以下具體問題修訂，不要整篇重寫】\n"
            f"{revision_instructions}"
        )

    system, user = load_prompt_parts(
        "topic_generate",
        newsletter_name=newsletter_name,
        topic_title=topic_title,
        content_type=content_type,
        content_type_name=_CONTENT_TYPE_NAMES[content_type],
        sources_text=build_sources_text(source_rows),
        revision_note=revision_note,
        opening_technique=opening_technique,
    )

    for attempt in range(_JSON_RETRIES + 1):
        response = create_chat_completion(
            client,
            model=get_writing_model(),
            max_tokens=2500,
            temperature=0.7,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **reasoning_effort_kwargs(),
        )
        raw_text = response.choices[0].message.content
        try:
            parsed = _parse_json_object(raw_text)
        except json.JSONDecodeError as exc:
            # 解析失敗也要把原始回應存下來，不然沒辦法回頭比對到底是哪裡壞的。
            # 每一次重擲都各留一筆，之後才能量失敗率有沒有真的下降。
            log_call("topic_generate", system, user, raw_text, None)
            if attempt == _JSON_RETRIES:
                raise
            print(
                f"[topic_generate]   JSON 解析失敗（{exc}），重新生成"
                f"（第 {attempt + 1}/{_JSON_RETRIES} 次）..."
            )
            continue
        # 保險絲：LLM 偶爾會在繁體輸出裡夾雜簡體字，這裡逐字元修正掉。
        parsed = fix_stray_simplified_in(parsed)
        log_call("topic_generate", system, user, raw_text, parsed)
        return parsed
