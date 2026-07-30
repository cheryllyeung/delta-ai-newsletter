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


# Groq 429 訊息格式是「Please try again in 6m26.208s」或「in 13.4s」，
# 分鐘是選填的；舊版正規表示式沒接分鐘，永遠對不上，只能退回指數退避。
_RETRY_DELAY_PATTERN = re.compile(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", re.IGNORECASE)
# 伺服器建議等待時間超過這個門檻，代表撞到的是每日額度（TPD）這種短時間內
# 不會恢復的限制，批次作業裡在這裡原地等沒有意義：等 5 次、每次頂格等，
# 反而比直接放棄還慢。這種情況立刻放棄，每一項本來就是冪等、下次重跑會
# 自動撿回來，比在這裡空等划算。只有「建議等待時間夠短」（分鐘級速率限制
# 快要解除）或完全沒帶建議時間（單純連線抖動）才值得原地重試。
_WORTH_RETRYING_DELAY = 20.0


def create_chat_completion(client: openai.OpenAI, *, max_retries: int = 5, **kwargs):
    """呼叫 chat.completions.create，撞到免費層速率限制（429）或連線層級錯誤
    （APIConnectionError，例如短暫斷線、對方主動斷開連線）時自動重試，而不是
    讓呼叫方直接把這篇文章/話題當失敗跳過。

    連線錯誤原本沒被接住：實測 ingest_topics 打分階段一撞到連線錯誤就整批
    放棄、一次重試都沒有，才發現這個洞。
    """
    for attempt in range(max_retries + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except (openai.RateLimitError, openai.APIConnectionError) as exc:
            match = _RETRY_DELAY_PATTERN.search(str(exc))
            suggested = (
                float(match.group(1) or 0) * 60 + float(match.group(2)) + 2 if match else 2 ** attempt
            )
            if attempt == max_retries or suggested > _WORTH_RETRYING_DELAY:
                raise
            print(f"[llm_client] {type(exc).__name__}，{suggested:.0f} 秒後重試（第 {attempt + 1} 次）...")
            time.sleep(suggested)
