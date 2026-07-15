"""呼叫 Claude API，把單一子領域的原始項目（arXiv 論文 + Reddit 討論）
整理成給該領域同仁看的電子報段落。

需要環境變數 ANTHROPIC_API_KEY。
"""
from __future__ import annotations

import json
import os

import anthropic

from ingestion.base import RawItem

MODEL = os.environ.get("NEWSLETTER_MODEL", "claude-sonnet-5")

_SYSTEM_PROMPT = """\
你是台達內部 AI 電子報的編輯助理。你的讀者是特定專業領域的工程師/主管，
不是一般大眾，所以摘要可以假設讀者具備該領域背景知識，重點放在「這個技術
進展/討論跟他們的工作有什麼關係」，而不是解釋基礎名詞。

寫作風格：
- 使用繁體中文（台灣用語）。
- 語氣專業、精簡，避免行銷式誇飾語言。
- 每則項目輸出：一句話標題翻譯/改寫、2-3句重點摘要、以及「為什麼跟{domain_name}相關」的一句話。
- 若原始項目是 Reddit 討論串（踩雷經驗、實作心得），摘要要保留「實務上的坑或教訓」，
  而不是只講技術本身。
- 只根據提供的資料摘要進行改寫，不要編造未提及的細節或數據。

輸出格式為 JSON 陣列，每個元素為：
{"title": "...", "summary": "...", "relevance": "...", "url": "...", "source_type": "arxiv 或 reddit"}
"""


def summarize_subdomain(
    domain_name: str,
    subdomain_name: str,
    subdomain_description: str,
    items: list[RawItem],
) -> list[dict]:
    """回傳這個子領域要放進電子報的條列項目（已經是 LLM 改寫過的繁中內容）。"""
    if not items:
        return []

    client = anthropic.Anthropic()

    items_payload = [
        {
            "title": item.title,
            "summary": item.summary[:800],
            "url": item.url,
            "source_type": item.source,
            "reddit_num_comments": item.extra.get("num_comments"),
        }
        for item in items
    ]

    user_prompt = f"""\
事業群/領域：{domain_name} - {subdomain_name}
領域說明：{subdomain_description}

以下是本週抓到的原始項目（JSON），請依照 system prompt 的規則改寫成電子報段落：

{json.dumps(items_payload, ensure_ascii=False, indent=2)}
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=_SYSTEM_PROMPT.replace("{domain_name}", f"{domain_name} - {subdomain_name}"),
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        # LLM 偶爾會在 JSON 外加說明文字，嘗試抓出第一個 [ 到最後一個 ] 之間的內容再解析一次
        start, end = raw_text.find("["), raw_text.rfind("]")
        if start != -1 and end != -1:
            return json.loads(raw_text[start : end + 1])
        raise
