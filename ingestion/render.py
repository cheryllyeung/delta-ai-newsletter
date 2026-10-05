"""用無頭瀏覽器取回需要 JavaScript 才看得到的頁面（2026-10-05 加）。

什麼時候需要這個：絕大多數來源用 requests 就夠。把 26 家廠商逐一試過之後，
真正需要瀏覽器的只有 Thermo Fisher 的發布頁（ASP.NET 的 postback，靜態
HTML 裡沒有文章連結）。先前以為需要瀏覽器的 Element Biosciences 與
Sophia Genetics，其實只是網址或介面選錯。

所以這一層刻意做成「要明確開啟才用」：設定裡寫 render: true 的來源才走
瀏覽器。瀏覽器每頁要多花幾秒、吃幾百 MB 記憶體，在這台沒有獨立顯卡的筆電上
不適合對 30 幾個來源全開。

三個實測得到的細節：

1. 一次抓取要共用同一個瀏覽器。每頁重開一次的話，列表頁加三篇文章就要開四
   次，實測會有頁面逾時、退回靜態抓取後拿到被擋的頁面（正文只有 46 字）。
2. 等待條件不能用 networkidle。有持續追蹤或輪詢請求的站永遠不會靜止，必定
   超時。改成等 DOM 載完再固定等幾秒。
3. 要自己指定 User-Agent。Playwright 預設帶 HeadlessChrome 字樣，有些站會
   據此擋掉。
"""
from __future__ import annotations

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


class Renderer:
    """一次抓取共用一個瀏覽器。用 with 包起來，離開時自動關掉。

    用法：
        with Renderer() as render:
            html = render(list_url)
            for url in urls:
                page_html = render(url)

    playwright 沒裝、或瀏覽器起不來時，`available` 會是 False，呼叫一律回
    空字串，呼叫端自己退回靜態抓取。
    """

    def __init__(self, timeout_ms: int = 40_000, settle_ms: int = 6_000) -> None:
        # 2026-10-05 把預設等待從 3.5 秒拉到 6 秒：Thermo Fisher 的發布頁內容
        # 是後載入的，3.5 秒拿到的是骨架（正文只有 46 字），6 秒才有 5,000 字。
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self._pw = None
        self._browser = None
        self._page = None

    @property
    def available(self) -> bool:
        return self._page is not None

    def __enter__(self) -> "Renderer":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[render] 沒裝 playwright，這個來源退回靜態抓取"
                  "（pip install playwright 後再 playwright install chromium）")
            return self
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_context(user_agent=_UA).new_page()
        except Exception as exc:  # noqa: BLE001 -- 起不來就退回靜態抓取
            print(f"[render] 瀏覽器啟動失敗，退回靜態抓取：{type(exc).__name__} {str(exc)[:70]}")
            self.__exit__(None, None, None)
        return self

    def __exit__(self, *exc_info) -> None:
        for closer in (
            lambda: self._browser.close() if self._browser else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 -- 關閉失敗不影響結果
                pass
        self._pw = self._browser = self._page = None

    def __call__(self, url: str) -> str:
        if not self.available:
            return ""
        try:
            self._page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            self._page.wait_for_timeout(self.settle_ms)
            return self._page.content()
        except Exception as exc:  # noqa: BLE001 -- 單頁失敗不中斷整批
            print(f"[render] {url[:70]} 渲染失敗，跳過：{type(exc).__name__}")
            return ""


def render_html(url: str, timeout_ms: int = 40_000, settle_ms: int = 3_500) -> str:
    """只渲染一頁的便利寫法（探測與除錯用，正式抓取請用 Renderer）。"""
    with Renderer(timeout_ms=timeout_ms, settle_ms=settle_ms) as render:
        return render(url)
