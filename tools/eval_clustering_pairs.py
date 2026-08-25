"""聚類門檻的固定測資評測：調 0.72 / 0.80 之前先跑這支看 before/after。

測資在 tests/data/clustering_pairs.yaml：19 組配對，都是池裡真實發生過的
案例（該併沒併的真同事件配對、還有曾經在 0.68 門檻下假合併的 GitHub repo
團塊）。same_event=true 理想上該併，false 絕對不能併。

文字快照跟 pipeline/topic_clustering.py 的 _text_for 一致（title + content
前 2000 字），embedding 用同一個本機模型，所以這裡算出來的相似度就是
聚類當下會看到的數字。

門檻預設讀 config/topics.yaml，可用參數蓋過來試新值：
    python -m tools.eval_clustering_pairs
    python -m tools.eval_clustering_pairs --threshold 0.70 --title-only-threshold 0.78
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 主控台 cp950 印不出部分字元會直接拋 UnicodeEncodeError，
# 跟 scripts/ingest_topics.py 同一個問題、同一個修法。
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from pipeline.embeddings import encode

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "data" / "clustering_pairs.yaml"
CONFIG_PATH = ROOT / "config" / "topics.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=None, help="一般門檻，預設讀 config")
    parser.add_argument(
        "--title-only-threshold", type=float, default=None, help="任一邊只有標題時的門檻，預設讀 config"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with open(CONFIG_PATH, encoding="utf-8") as f:
        embed_cfg = yaml.safe_load(f)["embedding"]
    threshold = args.threshold or embed_cfg["cluster_similarity_threshold"]
    title_only = args.title_only_threshold or embed_cfg.get(
        "cluster_similarity_threshold_title_only", threshold
    )

    with open(FIXTURE, encoding="utf-8") as f:
        fixture = yaml.safe_load(f)
    articles = fixture["articles"]
    pairs = fixture["pairs"]

    texts = {aid: f"{a['title']}\n\n{a['content']}" for aid, a in articles.items()}
    order = list(texts)
    vectors = encode([texts[aid] for aid in order])
    vec = {aid: vectors[i] for i, aid in enumerate(order)}

    print(f"門檻：一般 {threshold}、title-only {title_only}\n")
    header = f"{'相似度':>6}  {'門檻':>5}  {'判定':>4}  {'期望':>4}  {'':2}  說明"
    print(header)
    print("-" * 78)

    stats = {"true_merged": 0, "true_total": 0, "false_merged": 0, "false_total": 0}
    for pair in pairs:
        a, b = pair["a"], pair["b"]
        sim = float(vec[a] @ vec[b])
        either_signal = (
            articles[a]["gate_status"] == "signal_only" or articles[b]["gate_status"] == "signal_only"
        )
        used = title_only if either_signal else threshold
        merged = sim >= used
        expected = pair["same_event"]
        if expected:
            stats["true_total"] += 1
            stats["true_merged"] += merged
        else:
            stats["false_total"] += 1
            stats["false_merged"] += merged
        mark = "OK" if merged == expected else "XX"
        print(
            f"{sim:>6.3f}  {used:>5.2f}  {'併' if merged else '不併':>4}  "
            f"{'該併' if expected else '別併':>4}  {mark:2}  {pair['note']}"
        )

    print()
    print(f"真同事件配對：併了 {stats['true_merged']}/{stats['true_total']}（越高越好）")
    print(f"不相關配對：誤併 {stats['false_merged']}/{stats['false_total']}（必須是 0）")
    print()
    print("提醒：這 19 組是池裡撿的已知案例，不是隨機抽樣，數字代表「已知錯誤有沒有")
    print("修好」，不代表整體準確率。正式評測要等多家報導的題材累積。")


if __name__ == "__main__":
    main()
