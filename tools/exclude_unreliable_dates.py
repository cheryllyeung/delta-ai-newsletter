"""擋掉發布日期不可信的文章（2026-10-05 加）。

為什麼需要：爬蟲抓不到文章頁的日期時會退回「現在」，於是整個發布檔案庫都
被標成當天。實測 Thermo Fisher 一次灌進 40 篇，從第一季財報到股利公告全部
掛上今天的日期，而候選池只看日期，照收的話整期會被過期內容佔滿。

判準刻意保守：只擋「發布日等於抓取日」且來源在指定清單裡的那些。真的在當天
發布又當天抓到的文章會被誤擋，但那種情況下同一篇隔天還會再出現在列表頁，
損失有限；反過來放過假日期的代價是整期內容失真。

用法：
    python -m tools.exclude_unreliable_dates --sources vendor_thermo_fisher
    python -m tools.exclude_unreliable_dates --sources a,b --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.topic_db import get_connection

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_DATELINE = re.compile(
    r"(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),\s+(20\d\d)", re.I
)


def _dateline(content: str) -> date | None:
    """新聞稿開頭的發稿日，例如 "CHICAGO, July 30, 2026"。只看前 400 字，
    免得抓到文末的引用日期。"""
    m = _DATELINE.search(content[:600])
    if not m:
        return None
    try:
        key = m.group(1).lower()
        month = _MONTHS.get(key) or next(v for k, v in _MONTHS.items() if k.startswith(key))
        return date(int(m.group(3)), month, int(m.group(2)))
    except (ValueError, KeyError):
        return None

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, help="來源 id，逗號分隔")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fix", action="store_true",
                        help="找到真實日期時修正 published_at，而不是擋掉（建議優先用這個）")
    parser.add_argument("--also-undated", action="store_true",
                        help="連「頁面上找不到日期」的也擋掉（會誤殺日期其實正確的來源）")
    parser.add_argument("--wait", type=int, default=0, help="資料庫被鎖住時最多等幾秒")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))
    ids = [s.strip() for s in args.sources.split(",") if s.strip()]
    placeholders = ",".join("?" for _ in ids)

    # 兩種判準。第一種：發布日等於抓取日，表示頁面上找不到日期、退回了「現在」。
    # 第二種（2026-10-05 加）：內文的發稿日跟標記的發布日差太多。Tempus 的新聞頁
    # 把整個檔案庫都標成同一天（10-01），跟抓取日不同所以躲過第一種判準，但內文
    # 寫著 "CHICAGO, July 30, 2026"，實際上是兩個月前的舊聞。
    rows = conn.execute(
        f"""SELECT id, title, content, date(published_at) pub, date(fetched_at) fet
            FROM articles
            WHERE source_id IN ({placeholders})
              AND (gate_status IS NULL OR gate_status != 'excluded')""",
        ids,
    ).fetchall()
    # 預設只採用強證據：內文發稿日與標記的發布日不符。
    # 「頁面上找不到日期」不足以當判準，要加 --also-undated 才啟用：Fierce
    # Biotech 的文章頁也沒有日期，但它抓的是首頁的當日新聞，標記成當天是對的，
    # 一律擋掉會誤殺 98 篇（2026-10-05 實測）。
    suspects = []
    for row in rows:
        if args.also_undated and row["pub"] == row["fet"]:
            suspects.append((row, "頁面沒有日期，退回抓取當天"))
            continue
        real = _dateline(row["content"] or "")
        if real is None:
            continue
        if abs((date.fromisoformat(row["pub"]) - real).days) > 2:
            suspects.append((row, f"內文發稿日是 {real.isoformat()}，與標記的 {row['pub']} 不符"))
    rows = suspects
    print(f"[exclude_unreliable_dates] 符合條件 {len(rows)} 篇（來源：{'、'.join(ids)}）")
    for row, why in rows[:8]:
        print(f"    {(row['title'] or '')[:46]}　（{why}）")
    if args.dry_run or not rows:
        print("[exclude_unreliable_dates] 沒有寫入。")
        return

    if args.fix:
        fixed = 0
        for row, why in rows:
            real = _dateline(row["content"] or "")
            if real is None:
                continue
            conn.execute(
                "UPDATE articles SET published_at = ? WHERE id = ?",
                (real.isoformat() + "T00:00:00+00:00", row["id"]),
            )
            fixed += 1
        conn.commit()
        print(f"[exclude_unreliable_dates] 修正 {fixed} 篇的發布日期（其餘沒有可信日期，未動）。")
        return

    detail = json.dumps({"note": "發布日期不可信（見 tools/exclude_unreliable_dates.py 的兩種判準）"}, ensure_ascii=False)
    deadline = time.time() + args.wait
    while True:
        try:
            conn.executemany(
                """UPDATE articles SET gate_status='excluded', gate_reason='unreliable_date',
                   gate_detail_json=? WHERE id=?""",
                [(detail, row["id"]) for row, _ in rows],
            )
            conn.commit()
            break
        except Exception as exc:  # noqa: BLE001 -- 抓取還在寫入時會鎖住
            if time.time() >= deadline:
                print(f"[exclude_unreliable_dates] 資料庫鎖住，放棄：{str(exc)[:60]}")
                sys.exit(1)
            time.sleep(5)
    print(f"[exclude_unreliable_dates] 已擋掉 {len(rows)} 篇。")


if __name__ == "__main__":
    main()
