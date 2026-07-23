"""把 docs/ 底下的 markdown 轉成 PDF，檔名自動加上日期跟版本號
（例如 "0723 PRD v2.pdf"）。版本號依同資料夾內既有的同名 PDF 自動遞增。

用法：
    python -m scripts.export_pdf docs/PRD.md
    python -m scripts.export_pdf docs/status_report_2026-07-23.md "Status Report"

第二個參數可選，覆寫檔名裡的文件簡稱（預設用檔名去掉副檔名，並去掉結尾的
YYYY-MM-DD 日期字串）。

需要 pandoc（markdown -> html）跟 weasyprint（html -> pdf），兩者皆已透過
Homebrew 安裝在這台機器上。Mermaid 流程圖目前會被轉成純文字程式碼區塊，
不會渲染成圖，這是已知限制。
"""
from __future__ import annotations

import re
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


def main() -> None:
    md_path = Path(sys.argv[1]).resolve()
    name = sys.argv[2] if len(sys.argv) > 2 else re.sub(r"_\d{4}-\d{2}-\d{2}$", "", md_path.stem)

    out_dir = md_path.parent
    version = next_version(out_dir, name)
    out_path = out_dir / f"{date.today().strftime('%m%d')} {name} v{version}.pdf"

    html_body = subprocess.run(
        ["pandoc", str(md_path), "-f", "gfm", "-t", "html"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    html = _HTML_TEMPLATE.format(body=html_body)

    html_path = out_dir / f".{md_path.stem}.tmp.html"
    html_path.write_text(html, encoding="utf-8")
    try:
        subprocess.run(["weasyprint", str(html_path), str(out_path)], check=True)
    finally:
        html_path.unlink()

    print(f"[export_pdf] 已產出：{out_path}")


if __name__ == "__main__":
    main()
