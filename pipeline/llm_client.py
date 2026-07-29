"""建立 LLM client 的單一入口。

台達內部走 Clawith 包出來的 Qwen 模型端點，是 OpenAI 相容介面（不是
Anthropic 的 Messages API），所以這裡用 `openai` 套件，把 base_url 指向
公司內部 gateway。所有會呼叫 LLM 的模組都應該透過這支函式拿 client，
不要各自各寫一份，之後 gateway 網址或模型商換了只要改一個地方。
"""
from __future__ import annotations

import os
import re
import time

import openai

# Clawith 的 Qwen 端點實際 model 名稱要跟 Clawith 那邊確認（例如 qwen-plus /
# qwen-max 這類），先給一個明顯的預留值，避免不小心用錯值卻沒發現。
DEFAULT_MODEL = "REPLACE_WITH_CLAWITH_QWEN_MODEL_NAME"


def get_client() -> openai.OpenAI:
    return openai.OpenAI(
        api_key=os.environ.get("LLM_API_KEY"),
        base_url=os.environ.get("LLM_BASE_URL") or None,
    )


def get_model() -> str:
    return os.environ.get("NEWSLETTER_MODEL", DEFAULT_MODEL)


def get_writing_model() -> str:
    """寫作（generate）／自檢（self_check）這兩步的 prompt 帶了整篇來源全文，
    token 量遠大於標籤／打分，免費層 TPM 容易爆。允許用
    NEWSLETTER_WRITING_MODEL 指定一顆限額較寬鬆的小模型單獨扛這兩步，
    沒設就沿用跟其他階段一樣的 NEWSLETTER_MODEL。"""
    return os.environ.get("NEWSLETTER_WRITING_MODEL") or get_model()


_RETRY_DELAY_PATTERN = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)


def create_chat_completion(client: openai.OpenAI, *, max_retries: int = 5, **kwargs):
    """呼叫 chat.completions.create，撞到免費層速率限制（429）時照伺服器建議
    的秒數等待後自動重試，而不是讓呼叫方直接把這篇文章/話題當失敗跳過。
    """
    for attempt in range(max_retries + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except openai.RateLimitError as exc:
            if attempt == max_retries:
                raise
            match = _RETRY_DELAY_PATTERN.search(str(exc))
            delay = float(match.group(1)) + 2 if match else 2 ** attempt
            print(f"[llm_client] 速率限制，{delay:.0f} 秒後重試（第 {attempt + 1} 次）...")
            time.sleep(delay)
