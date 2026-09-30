"""把一期日報做成 Outlook 草稿信打開（不自動寄送）。

跑完會在螢幕上彈出一封排好版的新信件視窗，主旨與內文都填好，收件人
留白：寄不寄、寄給誰由使用者自己決定，按下傳送才會寄出。這是「手動
發信」的工作流；全自動寄送（排程直發收件名單）之後另外做。

信件內容直接重用 tools/render_issue_email.py 的渲染結果，兩邊永遠一致。

用法：
    python -m tools.draft_issue_email               # 最新一期
    python -m tools.draft_issue_email --issue-id 1
    python -m tools.draft_issue_email --issue-id 1 --attach "docs/0930 日報編輯脈絡 v1.pdf"
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _intro_to_html(text: str) -> str:
    """把純文字的信首訊息轉成跟 EDM 同字體的段落，插在信件最上方。
    Outlook 的 HTMLBody 是整個蓋掉的，人工很難在成品前面補字，所以
    開場白改由這裡帶進去（--intro 指到一個純文字檔，空行分段）。"""
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    sans = "'Microsoft JhengHei', 'PingFang TC', Arial, sans-serif"

    def emphasise(para: str) -> str:
        """開場白裡用 **包住** 的部分標成深藍粗體，跟日報裡的重點同一個顏色
        （2026-09-30 加）。純文字檔比較好改，但重點還是要看得出來。"""
        return re.sub(
            r"\*\*(.+?)\*\*",
            r'<span style="color:#1f4e79; font-weight:700;">\1</span>',
            para,
        )

    blocks = "".join(
        f'<div style="font-family:{sans}; font-size:14px; line-height:1.9; '
        'color:#1c2b38; margin:0 0 14px;">' + emphasise(p).replace("\n", "<br>") + "</div>"
        for p in paragraphs
    )
    # 2026-09-30 改成獨立的白底區塊：原本開場白直接落在信件的米白底上，
    # 跟日報版面連成一片，讀者分不出哪裡是寫信的人在講話、哪裡是日報本體。
    # 白底卡片加一條分隔線與「以下為日報內容」，上下就分得開了。
    return (
        '<table role="presentation" width="660" cellpadding="0" cellspacing="0" border="0" '
        'align="center" style="width:660px; max-width:100%; margin:0 auto;">'
        '<tr><td style="background:#ffffff; padding:26px 30px; border:1px solid #dfe3e8;">'
        + blocks
        + '<div style="border-top:1px solid #e6e9ed; margin-top:20px; padding-top:12px; '
        f'font-family:{sans}; font-size:12px; letter-spacing:2px; color:#8a8578;">'
        "以下為日報內容</div>"
        "</td></tr>"
        '<tr><td style="height:22px; line-height:22px; font-size:0;">&nbsp;</td></tr>'
        "</table>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    parser.add_argument("--intro", type=str, default=None, help="信首文字檔（純文字，空行分段），會排在 EDM 上方")
    parser.add_argument("--to", type=str, default=None, help="預填收件人；仍只開草稿不自動寄送")
    parser.add_argument(
        "--attach",
        action="append",
        default=None,
        help="附件路徑，可重複給。第一次寄給 NBDMD 時用來附上「日報編輯脈絡」PDF",
    )
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
        # 2026-09-18 修：原本靠找舊版模板的錨點字串插入，模板改版後錨點
        # 消失就靜默失敗（開場白整段不見）。改插在 <body> 標籤正後方，
        # 不依賴版型；連 <body> 都找不到就直接放最前面。
        import re

        intro_html = _intro_to_html(Path(args.intro).read_text(encoding="utf-8"))
        html, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + intro_html, html, count=1)
        if not n:
            html = intro_html + html

    # 主旨從檔名的日期組
    issue_date = html_path.stem.replace("email_preview_", "")

    import win32com.client

    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = MailItem
    mail.Subject = f"Delta 基因檢測日報（{issue_date}）"
    if args.to:
        mail.To = args.to
    mail.HTMLBody = html
    for path in args.attach or []:
        attachment = Path(path)
        if not attachment.exists():
            print(f"[draft_issue_email] 附件不存在，跳過：{attachment}")
            continue
        mail.Attachments.Add(str(attachment.resolve()))
        print(f"[draft_issue_email] 已附上：{attachment.name}")
    mail.Display()  # 打開草稿視窗，不寄送（送出由使用者自己按）
    print("[draft_issue_email] Outlook 草稿已打開，確認後自行按傳送。")


if __name__ == "__main__":
    main()
