"""把一期日報做成 Outlook 草稿信打開（不自動寄送）。

跑完會在螢幕上彈出一封排好版的新信件視窗，主旨與內文都填好，收件人
留白：寄不寄、寄給誰由使用者自己決定，按下傳送才會寄出。這是「手動
發信」的工作流；全自動寄送（排程直發收件名單）之後另外做。

信件內容直接重用 tools/render_issue_email.py 的渲染結果，兩邊永遠一致。

用法：
    python -m tools.draft_issue_email               # 最新一期
    python -m tools.draft_issue_email --issue-id 1
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _intro_to_html(text: str) -> str:
    """把純文字的信首訊息轉成跟 EDM 同字體的段落，插在信件最上方。
    Outlook 的 HTMLBody 是整個蓋掉的，人工很難在成品前面補字，所以
    開場白改由這裡帶進去（--intro 指到一個純文字檔，空行分段）。"""
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    blocks = "".join(
        '<div style="font-family:\'Microsoft JhengHei\', \'PingFang TC\', Arial, sans-serif; '
        'font-size:14px; line-height:1.9; color:#1c2b38; margin:0 0 14px;">'
        + p.replace("\n", "<br>")
        + "</div>"
        for p in paragraphs
    )
    return (
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" '
        'style="width:640px; max-width:100%; margin:0 auto 16px;"><tr><td style="padding:4px 8px;">'
        + blocks
        + "</td></tr></table>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    parser.add_argument("--intro", type=str, default=None, help="信首文字檔（純文字，空行分段），會排在 EDM 上方")
    args = parser.parse_args()

    # 先渲染（重用既有工具，確保跟預覽看到的完全相同）
    cmd = [sys.executable, "-X", "utf8", "-m", "tools.render_issue_email"]
    if args.issue_id:
        cmd += ["--issue-id", str(args.issue_id)]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)

    # 從輸出訊息取檔案路徑（render 工具印「已輸出：<path>」）
    out_line = next(line for line in result.stdout.splitlines() if "已輸出" in line)
    html_path = Path(out_line.split("：", 1)[1].split("（")[0].strip())
    html = html_path.read_text(encoding="utf-8")

    if args.intro:
        intro_html = _intro_to_html(Path(args.intro).read_text(encoding="utf-8"))
        marker = "<tr><td align=\"center\" style=\"padding:24px 12px;\">"
        html = html.replace(marker, marker + intro_html, 1)

    # 主旨從檔名的日期組
    issue_date = html_path.stem.replace("email_preview_", "")

    import win32com.client

    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = MailItem
    mail.Subject = f"Delta 基因檢測日報（{issue_date}）"
    mail.HTMLBody = html
    mail.Display()  # 打開草稿視窗，不寄送
    print("[draft_issue_email] Outlook 草稿已打開，填好收件人後自行按傳送。")


if __name__ == "__main__":
    main()
