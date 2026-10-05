"""備份並刪掉某一期，讓它可以用新素材重做（2026-10-05 加）。

什麼時候用：當天已經出刊、但來源或規則有變動，想用新素材重出同一期。
直接刪會失去原本的內容，所以先把整期寫成 JSON 存進 runs/，萬一新版本不如
舊版還能比對或還原。

刪掉的是 issues 那一列與它底下的 generated_topics。文章池與話題不動，所以
重新選題時那些話題照樣是候選。

用法：
    python -m tools.replace_issue --issue-id 12            # 備份並刪除
    python -m tools.replace_issue --issue-id 12 --dry-run  # 只看會刪什麼
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.topic_db import get_connection

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    parser.add_argument("--restore", default=None, help="從備份 JSON 還原整期")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))

    # 還原模式（2026-10-05 加）：重做之後若決定沿用舊版，要能把備份放回去，
    # 不然資料庫與實際寄出的內容會不一致，之後查帳對不上。
    if args.restore:
        data = json.loads(Path(args.restore).read_text(encoding="utf-8"))
        issue_row = data["issue"]
        cols = ", ".join(issue_row)
        marks = ", ".join("?" for _ in issue_row)
        conn.execute(f"INSERT INTO issues ({cols}) VALUES ({marks})", list(issue_row.values()))
        for row in data["generated_topics"]:
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO generated_topics ({cols}) VALUES ({marks})", list(row.values())
            )
            conn.execute(
                "UPDATE topics SET published_issue_id = ? WHERE id = ?",
                (issue_row["id"], row["topic_id"]),
            )
        conn.commit()
        print(f"[replace_issue] 已還原第 {issue_row['id']} 期（{issue_row['issue_date']}），"
              f"{len(data['generated_topics'])} 則。")
        return

    if args.issue_id is None:
        print("[replace_issue] 要給 --issue-id 或 --restore")
        sys.exit(1)
    issue = conn.execute("SELECT * FROM issues WHERE id = ?", (args.issue_id,)).fetchone()
    if issue is None:
        print(f"[replace_issue] 找不到第 {args.issue_id} 期。")
        sys.exit(1)

    topics = conn.execute(
        "SELECT * FROM generated_topics WHERE issue_id = ? ORDER BY id", (args.issue_id,)
    ).fetchall()
    backup = {
        "issue": {k: issue[k] for k in issue.keys()},
        "generated_topics": [{k: row[k] for k in row.keys()} for row in topics],
        "backed_up_at": datetime.now().isoformat(timespec="seconds"),
    }
    out = ROOT / "runs" / f"issue_{args.issue_id}_backup_{issue['issue_date']}.json"
    print(f"[replace_issue] 第 {args.issue_id} 期（{issue['issue_date']}），{len(topics)} 則")
    for row in topics:
        g = json.loads(row["generated_json"])
        print(f"    {g.get('chosen_headline', '')[:52]}")
    if args.dry_run:
        print("[replace_issue] dry-run，沒有刪除。")
        return

    out.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    # 參照 issues(id) 的有四處，刪之前都要先解開，不然外鍵會擋下來
    # （2026-10-05 實測 selection_trace 有 27 列、topics 的兩個欄位也指著它）。
    # selection_trace 的 issue_id 允許 NULL，所以改成 NULL 保留選題紀錄；
    # topics 的欄位清空等於「這個話題沒出刊過」，重新選題時才會再被考慮。
    conn.execute("UPDATE selection_trace SET issue_id = NULL WHERE issue_id = ?", (args.issue_id,))
    conn.execute(
        "UPDATE topics SET published_issue_id = NULL WHERE published_issue_id = ?", (args.issue_id,)
    )
    conn.execute(
        "UPDATE topics SET weekly_issue_id = NULL WHERE weekly_issue_id = ?", (args.issue_id,)
    )
    conn.execute("DELETE FROM generated_topics WHERE issue_id = ?", (args.issue_id,))
    conn.execute("DELETE FROM issues WHERE id = ?", (args.issue_id,))
    conn.commit()
    print(f"[replace_issue] 已備份到 {out.name} 並刪除，可以重新出刊這一天。")


if __name__ == "__main__":
    main()
