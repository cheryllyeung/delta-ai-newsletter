"""建立 Anthropic client 的單一入口。

台達內部走公司自己的 LLM gateway（不是打 api.anthropic.com），所以 base_url
要能被 .env 的 ANTHROPIC_BASE_URL 覆蓋。所有會呼叫 Claude 的模組都應該透過
這支函式拿 client，不要各自各寫一份 anthropic.Anthropic()，不然之後 gateway
網址換了要到處改。
"""
from __future__ import annotations

import os

import anthropic


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
    )
