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
import requests

# 本機 Ollama 預設監聽的 port，用來判斷目前是不是接本機備援模型。
_OLLAMA_MARKER = "11434"

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


def reasoning_effort_kwargs() -> dict:
    """`reasoning_effort` 是 Groq/Qwen 端點認得的參數，用來關掉輸出裡的
    `<think>` 推理過程（不關的話推理內容會佔掉 max_tokens 額度，甚至讓
    正式輸出被截斷成空字串）。但 Gemini 的 OpenAI 相容端點不認得這個參數，
    帶了會直接回 400 invalid argument，所以呼叫端不能寫死帶這個參數，要
    依目前 LLM_BASE_URL 是不是 Groq 才決定要不要帶，呼叫端用
    `**reasoning_effort_kwargs()` 展開。"""
    base_url = os.environ.get("LLM_BASE_URL", "")
    if "groq.com" in base_url:
        return {"reasoning_effort": "none"}
    return {}


def get_writing_model() -> str:
    """寫作（generate）／自檢（self_check）這兩步的 prompt 帶了整篇來源全文，
    token 量遠大於標籤／打分，免費層 TPM 容易爆。允許用
    NEWSLETTER_WRITING_MODEL 指定一顆限額較寬鬆的小模型單獨扛這兩步，
    沒設就沿用跟其他階段一樣的 NEWSLETTER_MODEL。"""
    return os.environ.get("NEWSLETTER_WRITING_MODEL") or get_model()


def get_review_model() -> str:
    """自檢（review/topic_selfcheck.py）步驟用的模型。DECISIONS.md 記過的洞：
    自檢原本沿用跟寫作同一顆模型，雖是獨立呼叫（不帶生成階段的對話歷史），
    但終究是同一個模型查自己的作業。允許用 NEWSLETTER_REVIEW_MODEL 指定另一顆
    模型當審查員，沒設就退回 get_writing_model()，行為跟改動前一致。"""
    return os.environ.get("NEWSLETTER_REVIEW_MODEL") or get_writing_model()


# Groq 429 訊息格式是「Please try again in 6m26.208s」或「in 13.4s」，
# 分鐘是選填的；舊版正規表示式沒接分鐘，永遠對不上，只能退回指數退避。
_RETRY_DELAY_PATTERN = re.compile(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", re.IGNORECASE)
# 伺服器建議等待時間超過這個門檻，代表撞到的是每日額度（TPD）這種短時間內
# 不會恢復的限制，批次作業裡在這裡原地等沒有意義：等 5 次、每次頂格等，
# 反而比直接放棄還慢。這種情況立刻放棄，每一項本來就是冪等、下次重跑會
# 自動撿回來，比在這裡空等划算。只有「建議等待時間夠短」（分鐘級速率限制
# 快要解除）或完全沒帶建議時間（單純連線抖動）才值得原地重試。
_WORTH_RETRYING_DELAY = 20.0


class _DuckTypedMessage:
    def __init__(self, content: str):
        self.content = content


class _DuckTypedChoice:
    def __init__(self, content: str):
        self.message = _DuckTypedMessage(content)


class _DuckTypedChatCompletion:
    """模仿 openai 的 ChatCompletion 回傳形狀（`.choices[0].message.content`），
    讓走 Ollama 原生 API 這條路的呼叫端不用另外判斷分支，程式碼可以完全共用。"""

    def __init__(self, content: str):
        self.choices = [_DuckTypedChoice(content)]


def _call_ollama_native(*, model: str, messages: list[dict], max_tokens: int, temperature: float | None):
    """Ollama 的 OpenAI 相容端點（/v1/chat/completions）不認得 `think` 這個
    參數，Qwen3 系列預設會先跑一大段推理過程才回答，實測單篇呼叫要 3 分鐘
    以上。改打 Ollama 原生的 /api/chat，帶 `think: false` 關掉推理，實測同一
    支呼叫降到 1 秒內，這個差距大到值得為本機備援單獨寫一條路徑。"""
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
    options = {"num_predict": max_tokens}
    if temperature is not None:
        options["temperature"] = temperature
    resp = requests.post(
        f"{root}/api/chat",
        json={"model": model, "messages": messages, "think": False, "stream": False, "options": options},
        # 寫作步驟要生成完整文章（上千 token 輸出），本機模型速度遠比雲端
        # API 慢，180 秒撐不住，實測會被截斷成逾時失敗，拉長到 10 分鐘。
        timeout=600,
    )
    resp.raise_for_status()
    return _DuckTypedChatCompletion(resp.json()["message"]["content"])


def create_chat_completion(client: openai.OpenAI, *, max_retries: int = 5, **kwargs):
    """呼叫 chat.completions.create，撞到免費層速率限制（429）或連線層級錯誤
    （APIConnectionError，例如短暫斷線、對方主動斷開連線）時自動重試，而不是
    讓呼叫方直接把這篇文章/話題當失敗跳過。

    連線錯誤原本沒被接住：實測 ingest_topics 打分階段一撞到連線錯誤就整批
    放棄、一次重試都沒有，才發現這個洞。
    """
    if _OLLAMA_MARKER in os.environ.get("LLM_BASE_URL", ""):
        return _call_ollama_native(
            model=kwargs["model"],
            messages=kwargs["messages"],
            max_tokens=kwargs.get("max_tokens", 2048),
            temperature=kwargs.get("temperature"),
        )

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
