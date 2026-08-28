"""一次性掃全池：把假合併的話題拆開，並修剪已生成文章的來源清單。

背景（2026-08-25）：title-only 文章的 embedding 塌在一起，聚類把七個互不
相關的 GitHub repo 併成一個話題，上刊文章下面掛了一排無關連結。聚類端已
改成灰色地帶問 LLM（pipeline/topic_clustering.py），但那只管以後，池裡
已經併錯的要用這支拆。

做法：對每個多文章話題做下面幾步。

1. 選錨點文章。話題有已生成文章時，錨點是「生成文章實際在寫的那一篇」
   （拿生成文的文字跟每個成員問 same_event_check，第一個判同的當錨點）；
   沒有就取最早的一篇。
2. 其餘成員逐一跟錨點問 same_event_check，判「不是同一件事」的拆出去
   自成新話題（Qdrant 的 topic_id payload 一併更新）。
3. 有拆動的話題：把它所有已生成文章的 source_article_ids_json 修剪成
   「還留在話題裡的文章」，被拆走的無關連結就從頁面上消失。
4. 有拆動的話題（含拆出來的新話題）打分歸零，之後重打。

用法：
    python -m tools.repair_false_merges            # 實際執行
    python -m tools.repair_false_merges --dry-run  # 只印判定，不動資料
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from pipeline import vector_store
from pipeline.llm_client import get_client
from pipeline.topic_clustering import _same_event
from pipeline.topic_db import create_topic, get_connection

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def _flatten_text(value) -> str:
    """把生成文章的 JSON 攤成純文字，給 same_event_check 當內容用。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(v) for v in value)
    return ""


class _FakeRow(dict):
    """給 _same_event 用的假 row：它只取 title 跟 content 兩個 key。"""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _pick_anchor(llm, members: list, generated_rows: list) -> object:
    """回傳錨點文章列。有生成文章時，錨點是生成文實際在寫的那一篇。"""
    if generated_rows:
        latest = generated_rows[-1]
        generated = json.loads(latest["generated_json"])
        gen_row = _FakeRow(
            title=_flatten_text(generated.get("title", "")) or "（無標題）",
            content=_flatten_text(generated)[:800],
        )
        for member in members:
            if _same_event(llm, gen_row, member):
                return member
    return members[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    conn = get_connection(config["database"]["path"])
    llm = get_client()
    vector_cfg = config["vector_store"]
    qdrant = None if args.dry_run else vector_store.get_client(vector_cfg["path"])

    multi_topics = conn.execute(
        """SELECT topic_id, COUNT(*) c FROM articles
           WHERE topic_id IS NOT NULL AND discarded_at IS NULL
           GROUP BY topic_id HAVING c > 1"""
    ).fetchall()
    print(f"[repair_false_merges] 多文章話題 {len(multi_topics)} 個，逐一檢查...")

    topics_touched = 0
    articles_split = 0
    trimmed_rows = 0
    for topic_row in multi_topics:
        topic_id = topic_row["topic_id"]
        members = conn.execute(
            """SELECT * FROM articles WHERE topic_id = ? AND discarded_at IS NULL
               ORDER BY published_at""",
            (topic_id,),
        ).fetchall()
        generated_rows = conn.execute(
            "SELECT * FROM generated_topics WHERE topic_id = ? ORDER BY created_at",
            (topic_id,),
        ).fetchall()

        anchor = _pick_anchor(llm, members, generated_rows)
        to_split = [
            m for m in members if m["id"] != anchor["id"] and not _same_event(llm, anchor, m)
        ]
        if not to_split:
            continue

        topics_touched += 1
        articles_split += len(to_split)
        print(f"[repair_false_merges] topic {topic_id}: 錨點「{anchor['title'][:40]}」，拆出 {len(to_split)} 篇：")
        for m in to_split:
            print(f"    - {m['title'][:60]}")
        if args.dry_run:
            continue

        for m in to_split:
            new_topic_id = create_topic(
                conn, representative_title=m["title"], seen_at=m["fetched_at"]
            )
            conn.execute("UPDATE articles SET topic_id = ? WHERE id = ?", (new_topic_id, m["id"]))
            vector_store.set_topic_for_articles(
                qdrant, vector_cfg["collection"], [m["id"]], new_topic_id
            )
        # 話題的成員變了，錨點話題的代表標題改回錨點自己的，分數歸零重打
        conn.execute(
            "UPDATE topics SET representative_title = ?, module_scores_json = NULL, content_type = NULL WHERE id = ?",
            (anchor["title"], topic_id),
        )

        remaining_ids = {m["id"] for m in members} - {m["id"] for m in to_split}
        for g in generated_rows:
            source_ids = json.loads(g["source_article_ids_json"])
            kept = [sid for sid in source_ids if sid in remaining_ids]
            if kept and set(kept) != set(source_ids):
                conn.execute(
                    "UPDATE generated_topics SET source_article_ids_json = ? WHERE id = ?",
                    (json.dumps(kept), g["id"]),
                )
                trimmed_rows += 1
        conn.commit()

    print(
        f"[repair_false_merges] 完成：動了 {topics_touched} 個話題、"
        f"拆出 {articles_split} 篇、修剪 {trimmed_rows} 篇生成文章的來源清單。"
    )
    if not args.dry_run and topics_touched:
        print("[repair_false_merges] 被動過的話題分數已歸零，記得重跑打分（ingest_topics 會自動撿）。")


if __name__ == "__main__":
    main()
