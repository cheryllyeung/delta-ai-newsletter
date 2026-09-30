"""只對指定日期範圍內的話題補打分（2026-09-30 加）。

為什麼需要這支：打分是 ingest 的最後一步，而「哪些話題可以打分」會被
收錄判定影響（話題底下還有文章沒標籤就不能打）。2026-09-30 的實際情況是
跑批當時只有 36 個話題可打分，回填新的收錄判定之後解鎖成 650 個，但那
650 個多數是舊話題，重跑整個 ingest 會把額度花在跟今天無關的內容上。

這支只打某個日期範圍內有文章的話題，用在「今天要出刊但候選不足」的場合。

用法：
    python -m tools.score_window --start 2026-09-29 --end 2026-09-30
    python -m tools.score_window --days 2            # 從今天往回兩天
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from pipeline.module_scoring import score_topic
from pipeline.topic_db import (
    get_articles_for_topic,
    get_connection,
    get_unscored_topics,
    save_module_scores,
)

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=None, help="起始日期（ISO date）")
    parser.add_argument("--end", default=date.today().isoformat(), help="結束日期（預設今天）")
    parser.add_argument("--days", type=int, default=2, help="沒給 --start 時，從 --end 往回幾天")
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    end = args.end
    start = args.start or (date.fromisoformat(end) - timedelta(days=args.days - 1)).isoformat()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))

    rows = get_unscored_topics(conn, date_range=(start, end))
    print(f"[score_window] {start} ~ {end} 待打分話題：{len(rows)} 個")
    if not rows:
        return

    # 話題底下的文章在主執行緒先讀好，worker 只做 LLM 呼叫、不碰連線
    # （sqlite3 的連線不是 thread-safe，照 ingest 的作法）。
    articles = {r["id"]: get_articles_for_topic(conn, r["id"]) for r in rows}

    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(score_topic, r, articles[r["id"]], config["modules"]): r for r in rows
        }
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                parsed = fut.result()
            except Exception as exc:  # noqa: BLE001 -- 單一話題失敗不中斷整批
                failed += 1
                print(f"[score_window]   打分失敗，跳過：{str(exc)[:70]}")
                continue
            save_module_scores(conn, row["id"], parsed["module_scores"], parsed["content_type"])
            done += 1
            if done % 10 == 0:
                print(f"[score_window]   已完成 {done}/{len(rows)}")
    conn.commit()
    print(f"[score_window] 完成：成功 {done} 個，失敗 {failed} 個。")


if __name__ == "__main__":
    main()
