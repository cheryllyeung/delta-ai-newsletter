"""把 docs/ 底下的 markdown 轉成 PDF，檔名自動加上日期跟版本號
（例如 "0723 PRD v2.pdf"）。版本號依同資料夾內既有的同名 PDF 自動遞增。

用法：
    python -m tools.export_pdf docs/PRD.md
    python -m tools.export_pdf docs/status_report_2026-07-23.md "Status Report"

第二個參數可選，覆寫檔名裡的文件簡稱（預設用檔名去掉副檔名，並去掉結尾的
YYYY-MM-DD 日期字串）。

兩條轉檔路徑，依機器上有什麼自動選：

1. pandoc + weasyprint（Mac 上用 Homebrew 裝的那組，優先用）
2. python-markdown + Chrome headless（2026-08-11 加，給沒有前者的機器用）

加第二條的原因是開發環境從 Mac 換到公司 Windows 筆電之後，pandoc 跟
weasyprint 都不在，而 weasyprint 在 Windows 要另外處理 GTK 相依，裝起來
不划算；Chrome 本來就有，而且 `--headless --print-to-pdf` 是內建功能，
零安裝。兩條路徑的版面不會完全一樣（CSS 一樣，但排版引擎不同），需要
逐頁對齊的正式文件還是建議在有 weasyprint 的機器上出。

Mermaid 流程圖目前會被轉成純文字程式碼區塊，不會渲染成圖，這是已知限制。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 2cm 1.8cm; }}
  body {{
    font-family: "Noto Sans TC", "Noto Sans Mono", "PingFang TC", "Heiti TC", "Microsoft JhengHei", sans-serif;
    font-size: 10.5pt; line-height: 1.7; color: #1c2b38;
  }}
  h1, h2, h3, h4 {{ line-height: 1.4; margin-top: 1.4em; margin-bottom: 0.5em; }}
  h1 {{ font-size: 20pt; border-bottom: 2px solid #2a5db0; padding-bottom: 6px; }}
  h2 {{ font-size: 15pt; color: #2a5db0; }}
  h3 {{ font-size: 12.5pt; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 9pt; }}
  th, td {{ border: 1px solid #dde3e8; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #eef1f4; white-space: nowrap; }}
  code {{ font-family: "JetBrains Mono", "Noto Sans Mono", monospace; background: #f4f6f8; padding: 1px 4px; border-radius: 3px; font-size: 9pt; }}
  pre {{ background: #f4f6f8; padding: 10px; border-radius: 4px; overflow-x: auto; font-size: 8.5pt; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ border-left: 3px solid #6f93cf; margin: 0.8em 0; padding: 0.2em 1em; color: #57707f; }}
  hr {{ border: none; border-top: 1px solid #dde3e8; margin: 1.5em 0; }}
  a {{ color: #2a5db0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def next_version(out_dir: Path, name: str) -> int:
    versions = []
    if (out_dir / f"{name}.pdf").exists():
        versions.append(1)
    for f in out_dir.glob(f"* {name} v*.pdf"):
        m = re.search(r"v(\d+)\.pdf$", f.name)
        if m:
            versions.append(int(m.group(1)))
    return max(versions, default=0) + 1


_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _find_chrome() -> str | None:
    for name in ("chrome", "google-chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for path in _CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


# 內嵌 SVG 流程圖用。python-markdown 不把 <svg> 當區塊級 HTML，會把它切碎
# 包進 <p> 標籤，SVG 結構就壞了（2026-08-14 實際踩到：整張流程圖被拆成
# 一段一段的純文字）。做法是轉換前先把 <svg>...</svg> 抽出來換成占位註解，
# markdown 轉完再放回去。
_SVG_BLOCK = re.compile(r"<svg\b.*?</svg>", re.S)


def md_to_html_body(md_path: Path) -> str:
    """markdown -> html body。優先用 pandoc，沒有就退回 python-markdown。"""
    if shutil.which("pandoc"):
        return subprocess.run(
            ["pandoc", str(md_path), "-f", "gfm", "-t", "html"],
            capture_output=True, text=True, check=True,
        ).stdout

    import markdown  # python-markdown，requirements.txt 已有的 jinja2 生態常見相依

    text = md_path.read_text(encoding="utf-8")
    svg_blocks: list[str] = []

    def _stash(match: re.Match) -> str:
        svg_blocks.append(match.group(0))
        return f"<!--SVGBLOCK{len(svg_blocks) - 1}-->"

    text = _SVG_BLOCK.sub(_stash, text)

    # tables/fenced_code 對應 gfm 的表格與 ``` 區塊，sane_lists 讓巢狀清單
    # 的行為接近 gfm；不裝 pymdown-extensions，避免為了出 PDF 增加相依。
    html = markdown.markdown(
        text, extensions=["tables", "fenced_code", "sane_lists", "toc"]
    )

    for i, svg in enumerate(svg_blocks):
        html = html.replace(f"<!--SVGBLOCK{i}-->", svg)
    return html


def html_to_pdf(html_path: Path, out_path: Path) -> str:
    """html -> pdf。優先用 weasyprint，沒有就用 Chrome headless。回傳用了哪一條。"""
    if shutil.which("weasyprint"):
        subprocess.run(["weasyprint", str(html_path), str(out_path)], check=True)
        return "weasyprint"

    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError(
            "找不到 weasyprint 也找不到 Chrome/Edge，無法轉 PDF。"
            "請安裝其中之一（Chrome 不需額外設定）。"
        )
    subprocess.run(
        [
            chrome, "--headless", "--disable-gpu", "--no-sandbox",
            # Chrome 的 --print-to-pdf 預設會自己加頁首頁尾（檔名、日期、頁碼），
            # 跟 weasyprint 那條路徑的版面不一致，關掉比較接近。
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_path}",
            html_path.resolve().as_uri(),
        ],
        check=True, capture_output=True,
    )
    if not out_path.exists():
        raise RuntimeError("Chrome 沒有產出 PDF，請確認它能存取暫存的 HTML 檔。")
    return Path(chrome).stem


def main() -> None:
    md_path = Path(sys.argv[1]).resolve()
    name = sys.argv[2] if len(sys.argv) > 2 else re.sub(r"_\d{4}-\d{2}-\d{2}$", "", md_path.stem)

    out_dir = md_path.parent
    version = next_version(out_dir, name)
    out_path = out_dir / f"{date.today().strftime('%m%d')} {name} v{version}.pdf"

    html = _HTML_TEMPLATE.format(body=md_to_html_body(md_path))

    html_path = out_dir / f".{md_path.stem}.tmp.html"
    html_path.write_text(html, encoding="utf-8")
    try:
        engine = html_to_pdf(html_path, out_path)
    finally:
        html_path.unlink()

    print(f"[export_pdf] 已產出（{engine}）：{out_path}")


if __name__ == "__main__":
    main()
