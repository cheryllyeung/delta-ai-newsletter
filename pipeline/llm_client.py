"""建立 LLM client 的單一入口。

台達內部走 Clawith 包出來的 Qwen 模型端點，是 OpenAI 相容介面（不是
Anthropic 的 Messages API），所以這裡用 `openai` 套件，把 base_url 指向
公司內部 gateway。所有會呼叫 LLM 的模組都應該透過這支函式拿 client，
不要各自各寫一份，之後 gateway 網址或模型商換了只要改一個地方。
"""
from __future__ import annotations

import os

import openai

# Clawith 的 Qwen 端點實際 model 名稱要跟 Clawith 那邊確認（例如 qwen-plus /
# qwen-max 這類），先給一個明顯的預留值，避免不小心用錯值卻沒發現。
DEFAULT_MODEL = "REPLACE_WITH_CLAWITH_QWEN_MODEL_NAME"


def get_client() -> openai.OpenAI:
    return openai.OpenAI(
        api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY"),
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("LLM_BASE_URL") or None,
    )


def get_model() -> str:
    return os.environ.get("NEWSLETTER_MODEL", DEFAULT_MODEL)
