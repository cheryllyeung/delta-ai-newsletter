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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
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
