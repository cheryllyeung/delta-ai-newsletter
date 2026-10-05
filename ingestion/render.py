"""用無頭瀏覽器取回需要 JavaScript 才看得到的頁面（2026-10-05 加）。

什麼時候需要這個：絕大多數來源用 requests 就夠，今天把 26 家廠商逐一試過
之後，真正需要瀏覽器的只有 Thermo Fisher 的發布頁（ASP.NET 的 postback
機制，靜態 HTML 裡沒有文章連結）。先前以為需要瀏覽器的 Element Biosciences
與 Sophia Genetics，其實只是網址或介面選錯。

所以這一層刻意做成「要明確開啟才用」：設定裡寫 render: true 的來源才走
瀏覽器，其餘來源不受影響。瀏覽器每頁要多花幾秒、吃幾百 MB 記憶體，在這台
沒有獨立顯卡的筆電上不適合對 30 幾個來源全開。

兩個實測得到的細節：

1. 等待條件不能用 networkidle。Thermo 與 Guardant 的站有持續的追蹤與輪詢
   請求，網路永遠不會靜止，45 秒必定超時。改成等 DOM 載完再固定等幾秒。
2. 要自己指定 User-Agent。Playwright 預設帶 HeadlessChrome 字樣，有些站
   會據此擋掉。
"""
from __future__ import annotations

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def render_html(url: str, timeout_ms: int = 40_000, settle_ms: int = 3_500) -> str:
    """回傳瀏覽器渲染後的 HTML，失敗回空字串（呼叫端自己退回靜態抓取）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[render] 沒裝 playwright，跳過渲染（pip install playwright 後再 playwright install chromium）")
        return ""

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=_UA)
                page = context.new_page()
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(settle_ms)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 -- 渲染失敗不該中斷整批抓取
        print(f"[render] {url} 渲染失敗，跳過：{type(exc).__name__} {str(exc)[:80]}")
        return ""
