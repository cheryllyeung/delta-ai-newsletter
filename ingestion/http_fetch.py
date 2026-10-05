"""取回網頁內容時的多重身分（2026-10-05 加）。

同一個網址對不同身分的反應不一樣，而且方向相反的例子都遇過：

- Endpoints News 擋簡短的程式 UA、接受瀏覽器 UA
- Fierce Biotech 與 Illumina 投資人新聞相反，擋瀏覽器 UA、接受簡短 UA
- MedTech Dive 兩種 UA 都擋，它認的是 TLS 握手指紋，要模擬 Chrome 才過

所以沒有一個「正確的 UA」，只能依序試。順序是先便宜後貴：兩種 requests
再退到模擬指紋的管道（那條會起一個 curl 實例，比較慢）。
"""
from __future__ import annotations

import requests

BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SIMPLE_UA = {"User-Agent": "delta-ai-newsletter/0.1"}


def get_bytes(url: str, timeout: int = 20) -> bytes | None:
    """回傳頁面內容（bytes），三種身分都失敗回 None。"""
    for headers in (BROWSER_UA, SIMPLE_UA):
        try:
            response = requests.get(url, timeout=timeout, headers=headers)
            if response.status_code == 200:
                return response.content
        except Exception:  # noqa: BLE001 -- 換下一種身分
            continue
    try:
        from curl_cffi import requests as cffi_requests

        alt = cffi_requests.get(url, impersonate="chrome", timeout=timeout + 10)
        if alt.status_code == 200:
            return alt.content
    except Exception:  # noqa: BLE001 -- 三種都不行
        pass
    return None


def get_text(url: str, timeout: int = 20) -> str:
    raw = get_bytes(url, timeout)
    if raw is None:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")
