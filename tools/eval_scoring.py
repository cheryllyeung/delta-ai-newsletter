"""打分評測：拿手標考題（tests/data/scoring_golden.yaml）驗模型打分準不準。

考題是 50 個話題，每題人工標三個欄位：真正相關的模組（relevant_modules）、
最重要的那個（top_module）、它值幾分（top_module_score，0/3/5/8/10 錨點）。
還沒標的題目自動跳過，標幾題算幾題，不用等 50 題標完才能跑。

量三個數字：

1. top-3 命中率：模型分數最高的前三個模組，有沒有交集到人標的相關模組。
   選題的模組輪動就是靠高分模組挑話題，前三名都不對，選題就是在亂選。
2. 高分紀律：人沒標相關的模組裡，模型打 8 分以上的比例。這就是「最高分
   中位數 9.5、門檻擋不到東西」的直接量化，rubric 錨點就是為了壓這個。
3. 分數相關性：人標的 top_module_score 跟模型在同一個模組的分數，
   跨題目算 Spearman。看的是模型至少能不能把「重要的排在不重要前面」。

預設拿資料庫裡現存的分數來評（舊 prompt 打的，當 before）。加 --fresh
會用目前的 prompt 重打一次（當 after），兩欄並排就是 before/after 對照表。

用法：
    python -m tools.eval_scoring            # 只評資料庫現存分數
    python -m tools.eval_scoring --fresh    # 加跑一輪現行 prompt，出對照
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.module_scoring import score_topic
from pipeline.topic_db import get_articles_for_topic, get_connection
from scripts.ingest_topics import _run_llm_concurrently

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "data" / "scoring_golden.yaml"
CONFIG_PATH = ROOT / "config" / "topics.yaml"


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman 相關（同分取平均名次），n 只有幾十，不拉 scipy。"""

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        rank = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rank[order[k]] = avg
            i = j + 1
        return rank

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = sum((a - mx) ** 2 for a in rx) ** 0.5
    sy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (sx * sy) if sx and sy else 0.0


def evaluate(labeled: list[dict], scores_by_topic: dict[int, dict]) -> dict:
    hits = 0
    false_high = false_high_total = 0
    human_scores, model_scores = [], []
    evaluated = 0
    for item in labeled:
        scores = scores_by_topic.get(item["topic_id"])
        if not scores:
            continue
        evaluated += 1
        top3 = sorted(scores, key=lambda mid: scores[mid]["score"], reverse=True)[:3]
        if set(top3) & set(item["relevant_modules"]):
            hits += 1
        for mid, entry in scores.items():
            if mid not in item["relevant_modules"]:
                false_high_total += 1
                if entry["score"] >= 8:
                    false_high += 1
        if item["top_module"] in scores:
            human_scores.append(item["top_module_score"])
            model_scores.append(scores[item["top_module"]]["score"])
    return {
        "evaluated": evaluated,
        "top3_hit": f"{hits}/{evaluated}",
        "false_high_rate": false_high / false_high_total if false_high_total else 0.0,
        "spearman": spearman(human_scores, model_scores) if len(human_scores) >= 5 else None,
        "n_score_pairs": len(human_scores),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="用現行 prompt 重打一輪當 after")
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    with open(GOLDEN, encoding="utf-8") as f:
        golden = yaml.safe_load(f)

    labeled = [
        t
        for t in golden["topics"]
        if t["relevant_modules"] is not None
        and t["top_module"]
        and t["top_module_score"] is not None
    ]
    # relevant_modules 允許標成空清單（真的跟哪個模組都無關的話題），
    # 但那樣 top_module 也該是空，兩個欄位不一致的題目直接不算。
    print(f"[eval_scoring] 考題 {len(golden['topics'])} 題，已標好 {len(labeled)} 題。")
    if not labeled:
        print("還沒有標好的題目。打開 tests/data/scoring_golden.yaml 照最上面的說明標。")
        return

    conn = get_connection(config["database"]["path"])
    db_scores: dict[int, dict] = {}
    topic_rows: dict[int, object] = {}
    for item in labeled:
        row = conn.execute("SELECT * FROM topics WHERE id = ?", (item["topic_id"],)).fetchone()
        if row and row["module_scores_json"]:
            db_scores[item["topic_id"]] = json.loads(row["module_scores_json"])
            topic_rows[item["topic_id"]] = row

    results = {"資料庫現存分數": evaluate(labeled, db_scores)}

    if args.fresh:
        fresh_scores: dict[int, dict] = {}
        jobs = [
            {"topic": topic_rows[item["topic_id"]], "topic_id": item["topic_id"]}
            for item in labeled
            if item["topic_id"] in topic_rows
        ]

        def _save(job, parsed) -> None:
            fresh_scores[job["topic_id"]] = parsed["module_scores"]

        _run_llm_concurrently(
            jobs,
            work=lambda job: score_topic(
                job["topic"], get_articles_for_topic(get_connection(config["database"]["path"]), job["topic_id"]), config["modules"]
            ),
            handle=_save,
            describe=lambda job: job["topic"]["representative_title"][:60],
            concurrency=args.concurrency,
            label="重打",
        )
        results["現行 prompt 重打"] = evaluate(labeled, fresh_scores)

    print()
    print(f"{'':<16} {'top-3 命中':>10} {'高分紀律(越低越好)':>18} {'Spearman':>9} {'配對數':>6}")
    for name, r in results.items():
        sp = f"{r['spearman']:.3f}" if r["spearman"] is not None else "樣本不足"
        print(
            f"{name:<16} {r['top3_hit']:>10} {r['false_high_rate'] * 100:>17.1f}% {sp:>9} {r['n_score_pairs']:>6}"
        )
    print()
    print("高分紀律 = 人沒標相關的模組裡模型打 8 分以上的比例。rubric 錨點的目標")
    print("就是把這個數字壓下來，同時 top-3 命中不能跟著掉。")


if __name__ == "__main__":
    main()
