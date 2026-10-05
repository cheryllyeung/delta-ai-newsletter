"""針對某一期重寫重複度過高的摘要（2026-10-05 加）。

邏輯在 pipeline/card_summary.py，出刊流程每期會自動跑一次，這支用在
「某一期已經出刊、想單獨重整摘要」的場合。

用法：
    python -m tools.rewrite_card_summaries --issue-id 11
    python -m tools.rewrite_card_summaries --issue-id 11 --dry-run
    python -m tools.rewrite_card_summaries              # 最新一期
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from pipeline.card_summary import rewrite_duplicative_summaries
from pipeline.summary_overlap import THRESHOLD
from pipeline.topic_db import get_connection

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    parser.add_argument("--dry-run", action="store_true", help="只報告，不寫回資料庫")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))
    if args.issue_id:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (args.issue_id,)).fetchone()
    else:
        issue = conn.execute(
            "SELECT * FROM issues ORDER BY issue_date DESC, id DESC LIMIT 1"
        ).fetchone()
    if issue is None:
        print("[rewrite_card_summaries] 找不到這一期。")
        sys.exit(1)

    changed, kept = rewrite_duplicative_summaries(
        conn, issue["id"], threshold=args.threshold, dry_run=args.dry_run
    )
    print(
        f"[rewrite_card_summaries] 改寫 {changed} 則，保留原本 {kept} 則"
        + ("（dry-run，沒有寫回）" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
