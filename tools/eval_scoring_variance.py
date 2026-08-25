"""打分重複性測試：同一批話題重打幾次，量分數會漂多少。

為什麼要量這個：選題的三輪演算法（模組輪動、總分補位、保底）全部建立在
18 模組分數上。如果同一個話題重打一次分數就大幅變動，那排序本身不穩，
討論門檻跟配額都是空的。這也是打分評測集（手標考題）的前置：漂移大的話，
評測時每題要多次取平均才有意義。

量三個東西：
1. 分數漂移：每個「話題×模組」格子在多次重打之間的極差（max-min）分布
2. 模組排序穩定：同一話題兩次重打的 18 模組排序 Kendall tau。
   這影響 dominant_group（用最高分模組代表話題歸屬本業／職能）穩不穩
3. 話題排序穩定：用跨模組總分排 20 個話題，兩次重打之間的 Kendall tau。
   這影響補位輪的先後順序

抽樣是固定 seed 的隨機抽樣，同一個池跑兩次會抽到同一批話題，結果可比。
分數寫進 runs/scoring_variance_<日期>.json 留檔，不動 topics 表。

用法：
    python -m tools.eval_scoring_variance                  # 預設 20 話題 × 3 次
    python -m tools.eval_scoring_variance --topics 5 --runs 2   # 先小跑試水
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import gates
from pipeline.module_scoring import score_topic
from pipeline.topic_db import get_articles_for_topic, get_connection
from scripts.ingest_topics import _run_llm_concurrently

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
SEED = 42


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def kendall_tau(order_a: list, order_b: list) -> float:
    """兩個排序的 Kendall tau（-1 到 1，1 是完全一致）。項目少（18 或 20），
    O(n^2) 的樸素算法就夠，不為這個拉 scipy 進相依。"""
    assert set(order_a) == set(order_b)
    pos_b = {item: i for i, item in enumerate(order_b)}
    concordant = discordant = 0
    for x, y in itertools.combinations(order_a, 2):
        # x 在 order_a 裡排在 y 前面；看 order_b 是否同意
        if pos_b[x] < pos_b[y]:
            concordant += 1
        else:
            discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topics", type=int, default=20, help="抽幾個話題")
    parser.add_argument("--runs", type=int, default=3, help="每個話題重打幾次")
    parser.add_argument("--concurrency", type=int, default=8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    conn = get_connection(config["database"]["path"])

    # 候選：已打過分、底下有可寫作文章的話題（跟真正進選題的條件一致）
    topic_rows = conn.execute(
        "SELECT * FROM topics WHERE module_scores_json IS NOT NULL ORDER BY id"
    ).fetchall()
    eligible = []
    for row in topic_rows:
        articles = get_articles_for_topic(conn, row["id"])
        if gates.substantive(articles):
            eligible.append((row, articles))
    print(f"[eval_scoring_variance] 可抽樣話題：{len(eligible)} 個")

    sample = random.Random(SEED).sample(eligible, min(args.topics, len(eligible)))
    jobs = [
        {"topic": row, "articles": articles, "run_index": run}
        for row, articles in sample
        for run in range(args.runs)
    ]
    print(f"[eval_scoring_variance] {len(sample)} 話題 × {args.runs} 次 = {len(jobs)} 次打分")

    results: dict[int, dict[int, dict]] = {}  # topic_id -> run_index -> parsed

    def _save(job, parsed) -> None:
        results.setdefault(job["topic"]["id"], {})[job["run_index"]] = parsed

    ok, failed = _run_llm_concurrently(
        jobs,
        work=lambda job: score_topic(job["topic"], job["articles"], config["modules"]),
        handle=_save,
        describe=lambda job: f"{job['topic']['representative_title'][:50]} (run {job['run_index'] + 1})",
        concurrency=args.concurrency,
        label="重打",
    )
    print(f"[eval_scoring_variance] 完成 {ok} 次，失敗 {failed} 次。")

    complete = {tid: runs for tid, runs in results.items() if len(runs) == args.runs}
    if len(complete) < len(results):
        print(f"  有 {len(results) - len(complete)} 個話題缺 run（呼叫失敗），只統計跑滿的 {len(complete)} 個。")
    if not complete:
        print("  沒有任何話題跑滿，無法統計。")
        return

    # 1. 分數漂移：每個 話題×模組 格子的極差
    ranges: list[float] = []
    per_topic_max_range: dict[int, float] = {}
    for tid, runs in complete.items():
        module_ids = runs[0]["module_scores"].keys()
        topic_ranges = []
        for mid in module_ids:
            scores = [runs[r]["module_scores"][mid]["score"] for r in runs]
            topic_ranges.append(max(scores) - min(scores))
        ranges.extend(topic_ranges)
        per_topic_max_range[tid] = max(topic_ranges)

    # 2. 模組排序穩定（含 top-1 模組一致率，dominant_group 就看它）
    module_taus: list[float] = []
    top1_stable = top1_total = 0
    group_map = {
        m["id"]: group for group in ("functional", "domain") for m in config["modules"][group]
    }
    group_stable = 0
    for tid, runs in complete.items():
        orders = []
        top_modules = []
        for r in sorted(runs):
            ms = runs[r]["module_scores"]
            order = sorted(ms, key=lambda mid: ms[mid]["score"], reverse=True)
            orders.append(order)
            top_modules.append(order[0])
        for a, b in itertools.combinations(orders, 2):
            module_taus.append(kendall_tau(a, b))
        top1_total += 1
        if len(set(top_modules)) == 1:
            top1_stable += 1
        if len({group_map[m] for m in top_modules}) == 1:
            group_stable += 1

    # 3. 話題排序穩定：各 run 用跨模組總分排一次
    topic_taus: list[float] = []
    run_orders = []
    for r in range(args.runs):
        totals = {
            tid: sum(e["score"] for e in runs[r]["module_scores"].values())
            for tid, runs in complete.items()
        }
        run_orders.append(sorted(totals, key=totals.get, reverse=True))
    for a, b in itertools.combinations(run_orders, 2):
        topic_taus.append(kendall_tau(a, b))

    range_counter = Counter(
        "0" if r == 0 else "0-1" if r <= 1 else "1-2" if r <= 2 else "2-3" if r <= 3 else ">3"
        for r in ranges
    )
    print()
    print(f"=== 分數漂移（{len(complete)} 話題 × 18 模組，每格重打 {args.runs} 次的極差）===")
    for bucket in ("0", "0-1", "1-2", "2-3", ">3"):
        n = range_counter.get(bucket, 0)
        print(f"  極差 {bucket:>4} 分：{n:>4} 格（{n / len(ranges) * 100:.0f}%）")
    print(f"  平均極差 {statistics.mean(ranges):.2f}，中位數 {statistics.median(ranges):.2f}，最大 {max(ranges):.1f}")
    print()
    print("=== 模組排序穩定（同話題兩兩 run 之間）===")
    print(f"  Kendall tau 平均 {statistics.mean(module_taus):.3f}，最低 {min(module_taus):.3f}")
    print(f"  最高分模組不變：{top1_stable}/{top1_total} 個話題")
    print(f"  dominant_group（本業／職能歸屬）不變：{group_stable}/{top1_total} 個話題")
    print()
    print("=== 話題排序穩定（跨模組總分排序，兩兩 run 之間）===")
    print(f"  Kendall tau 平均 {statistics.mean(topic_taus):.3f}，最低 {min(topic_taus):.3f}")

    out_dir = Path("runs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"scoring_variance_{datetime.now(timezone.utc).date().isoformat()}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "seed": SEED,
                "runs": args.runs,
                "topics": {
                    str(tid): {
                        "title": next(r["representative_title"] for r, _ in sample if r["id"] == tid),
                        "max_range": per_topic_max_range[tid],
                        "scores": {
                            str(run): {
                                mid: entry["score"]
                                for mid, entry in parsed["module_scores"].items()
                            }
                            for run, parsed in runs.items()
                        },
                    }
                    for tid, runs in complete.items()
                },
                "summary": {
                    "mean_range": statistics.mean(ranges),
                    "median_range": statistics.median(ranges),
                    "max_range": max(ranges),
                    "module_tau_mean": statistics.mean(module_taus),
                    "top1_stable": f"{top1_stable}/{top1_total}",
                    "group_stable": f"{group_stable}/{top1_total}",
                    "topic_tau_mean": statistics.mean(topic_taus),
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n原始分數留檔：{out_path}")


if __name__ == "__main__":
    main()
