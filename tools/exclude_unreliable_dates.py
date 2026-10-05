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
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.topic_db import get_connection

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, help="來源 id，逗號分隔")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait", type=int, default=0, help="資料庫被鎖住時最多等幾秒")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))
    ids = [s.strip() for s in args.sources.split(",") if s.strip()]
    placeholders = ",".join("?" for _ in ids)

    rows = conn.execute(
        f"""SELECT id, title FROM articles
            WHERE source_id IN ({placeholders})
              AND date(published_at) = date(fetched_at)
              AND (gate_status IS NULL OR gate_status != 'excluded')""",
        ids,
    ).fetchall()
    print(f"[exclude_unreliable_dates] 符合條件 {len(rows)} 篇（來源：{'、'.join(ids)}）")
    for row in rows[:5]:
        print(f"    {(row['title'] or '')[:60]}")
    if args.dry_run or not rows:
        print("[exclude_unreliable_dates] 沒有寫入。")
        return

    detail = json.dumps(
        {"note": "頁面沒有結構化日期欄位，發布日被預設成抓取當天，不可信"}, ensure_ascii=False
    )
    deadline = time.time() + args.wait
    while True:
        try:
            conn.executemany(
                """UPDATE articles SET gate_status='excluded', gate_reason='unreliable_date',
                   gate_detail_json=? WHERE id=?""",
                [(detail, row["id"]) for row in rows],
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
