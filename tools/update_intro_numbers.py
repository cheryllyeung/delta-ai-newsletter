"""把開場白裡會變動的數字換成當期實際值（2026-10-05 加）。

開場白是手寫的文字檔，裡面有幾個數字會隨當期改變（則數、今天掃進來幾篇、
與名單廠商直接相關幾則）。重做同一期之後這些數字就過期了，而過期的數字
比沒有數字更糟，所以用這支照資料庫改好，另存成 _v2 版本，原稿不動。

用法：python tools/update_intro_numbers.py 2026-10-05
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.edm_tags import vendor_tags

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    issue_date = sys.argv[1] if len(sys.argv) > 1 else None
    if not issue_date:
        print("[update_intro] 要給出刊日期，例如 2026-10-05")
        sys.exit(1)

    src = ROOT / "runs" / f"intro_{issue_date}.txt"
    if not src.exists():
        print(f"[update_intro] 找不到開場白：{src.name}")
        sys.exit(1)

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = sqlite3.connect(str(ROOT / "data" / Path(config["database"]["path"]).name))
    conn.row_factory = sqlite3.Row
    issue = conn.execute(
        "SELECT * FROM issues WHERE issue_date = ? ORDER BY id DESC LIMIT 1", (issue_date,)
    ).fetchone()
    if issue is None:
        print(f"[update_intro] {issue_date} 沒有期數")
        sys.exit(1)

    rows = conn.execute(
        "SELECT topic_id FROM generated_topics WHERE issue_id = ?", (issue["id"],)
    ).fetchall()
    published = len(rows)
    scanned = conn.execute(
        "SELECT count(*) c FROM articles WHERE date(fetched_at) = ?", (issue_date,)
    ).fetchone()["c"]

    hits = 0
    for row in rows:
        arts = conn.execute(
            """SELECT title, content FROM articles
               WHERE topic_id = ? AND discarded_at IS NULL
                 AND (gate_status IS NULL OR gate_status != 'excluded')""",
            (row["topic_id"],),
        ).fetchall()
        if any(
            vendor_tags((a["title"] or "") + "\n" + (a["content"] or ""), config) for a in arts
        ):
            hits += 1

    text = src.read_text(encoding="utf-8")
    # 這三處是會過期的數字，逐一換掉。找不到就不動，寧可留舊字也不要改錯位置。
    text = re.sub(r"共 \*\*\d+ 則\*\*", f"共 **{published} 則**", text, count=1)
    text = re.sub(r"今天一共抓進來 [\d,]+ 篇", f"今天一共抓進來 {scanned} 篇", text, count=1)
    text = re.sub(
        r"直接相關的報導是 \*\*\d+ 則\*\*",
        f"直接相關的報導是 **{hits} 則**",
        text,
        count=1,
    )
    out = ROOT / "runs" / f"intro_{issue_date}_v2.txt"
    out.write_text(text, encoding="utf-8")
    print(f"[update_intro] 已寫出 {out.name}：則數 {published}、掃進來 {scanned} 篇、名單廠商相關 {hits} 則")
    print("[update_intro] 其餘段落（分類說明、預印本則數、舊日期那則）請自行確認是否仍成立")


if __name__ == "__main__":
    main()
