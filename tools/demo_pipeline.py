"""錄影用的完整鏈路示範：爬蟲 → 收錄判定 → 聚類 → 標籤 → 打分 → 選題
→ 生成 → 自檢 → 渲染 EDM 與網頁。2026-09-23 加。

為什麼要這支：正式每日跑批一次抓十幾個來源、上百篇文章，聚類加生成要
幾十分鐘，沒辦法對著鏡頭現場跑。這支把範圍縮到兩三個來源、少數幾篇，
其餘每一步都走正式程式碼（不是模擬），所以錄出來的就是真的流程。

跟正式跑批的差別只有三個，都在縮短時間：
  1. --only-sources 只抓指定來源（正式是全部）
  2. --limit 只標籤與打分前 N 篇（正式是全跑）
  3. 出刊日期預設用今天，已經出過就用 --date 指定別天

每一段之間會停下來等 Enter，方便分鏡拍攝；--no-pause 可一路跑完。

用法：
    python -m tools.demo_pipeline                       # 預設來源與範圍
    python -m tools.demo_pipeline --no-pause            # 不等按鍵
    python -m tools.demo_pipeline --date 2026-09-24     # 指定出刊日期
    python -m tools.demo_pipeline --only-sources geneonline,fierce_biotech
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BOLD, DIM, CYAN, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[0m",
)

# 示範預設來源：挑更新頻率高、內文抓得完整的兩個，抓一次約一分鐘。
_DEFAULT_SOURCES = "geneonline,fierce_biotech"


def _banner(no: int, total: int, title: str, why: str) -> None:
    print()
    print(f"{CYAN}{'═' * 70}{RESET}")
    print(f"{CYAN}{BOLD} {no}/{total}　{title}{RESET}")
    print(f"{DIM} {why}{RESET}")
    print(f"{CYAN}{'═' * 70}{RESET}")


def _run(cmd: list[str], why: str, no: int, total: int, title: str, pause: bool) -> int:
    _banner(no, total, title, why)
    print(f"{DIM}$ {' '.join(cmd)}{RESET}\n")
    code = subprocess.call(cmd, cwd=str(ROOT))
    if code != 0:
        print(f"\n{YELLOW}這一步結束時回傳 {code}，後面的步驟仍會繼續。{RESET}")
    if pause:
        input(f"\n{DIM}（按 Enter 進行下一步）{RESET}")
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat(), help="出刊日期（預設今天）")
    parser.add_argument("--only-sources", default=_DEFAULT_SOURCES, help="只抓這些來源 id，逗號分隔")
    parser.add_argument("--limit", type=int, default=6, help="只標籤與打分前 N 篇／N 個（預設 6）")
    parser.add_argument("--no-pause", action="store_true", help="不等按鍵，一路跑完")
    parser.add_argument("--port", type=int, default=8002, help="最後啟動網頁的埠號")
    args = parser.parse_args()
    pause = not args.no_pause
    py = [sys.executable, "-X", "utf8", "-u", "-m"]

    print(f"\n{BOLD}基因檢測日報　pipeline 完整示範{RESET}")
    print(f"{DIM}來源：{args.only_sources}　範圍：前 {args.limit} 篇　出刊日期：{args.date}{RESET}")
    print(f"{DIM}每一步都走正式程式碼，只有抓取範圍與處理篇數被縮小。{RESET}")
    if pause:
        input(f"\n{DIM}（按 Enter 開始）{RESET}")

    total = 3

    # 1：爬蟲＋收錄判定＋聚類＋標籤＋打分（都在 ingest 裡，畫面會逐步印出）
    _run(
        py + ["scripts.ingest_topics", "--only-sources", args.only_sources,
              "--limit", str(args.limit), "--concurrency", "4"],
        "抓 RSS 與內文 → 太舊／只有標題／不相關的擋掉 → 同事件併成一則話題 → 打標籤 → 對各業務面向打分",
        1, total, "爬蟲與分析", pause,
    )

    # 2：選題＋生成＋自檢＋出刊（compose 內含重寫迴圈，畫面會印信心度）
    _run(
        py + ["scripts.compose_topic_issue", "--date", args.date, "--cadence", "daily"],
        "過門檻的才寫成文章；寫完獨立自檢一次，信心度不夠就回頭重寫，仍不過就撤下不出刊",
        2, total, "選題、生成與自檢", pause,
    )

    # 3：渲染 EDM
    _run(
        py + ["tools.render_issue_email"],
        "把這一期渲染成 Outlook 讀得動的 EDM 信件，檔案落在 runs/ 底下，用瀏覽器開就是收件人看到的版面",
        3, total, "渲染 EDM 信件", pause,
    )

    # 收尾：啟動網頁（這步會卡住不返回，所以放最後，並先把網址印出來）
    print()
    print(f"{CYAN}{'═' * 70}{RESET}")
    print(f"{CYAN}{BOLD} 收尾　網頁版{RESET}")
    print(f"{DIM} 同一份內容的網頁版：首頁、單期頁、單篇全文、選題帳{RESET}")
    print(f"{CYAN}{'═' * 70}{RESET}")
    print(f"\n  EDM 預覽檔：{ROOT / 'runs' / f'email_preview_{args.date}.html'}")
    print(f"  網頁：{GREEN}http://127.0.0.1:{args.port}{RESET}")
    print(f"\n{DIM}（啟動網頁後這支不會結束，錄完按 Ctrl+C 停止）{RESET}\n")
    if pause:
        input(f"{DIM}（按 Enter 啟動網頁）{RESET}")
    subprocess.call(py + ["scripts.serve_topics", "--port", str(args.port)], cwd=str(ROOT))


if __name__ == "__main__":
    main()
