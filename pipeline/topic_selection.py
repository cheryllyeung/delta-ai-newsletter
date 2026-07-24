"""階段三：對已打分的話題池做組合最佳化選題（依 docs/0724_v3_PRD.md 階段三）。

跟打分（pipeline/module_scoring.py）完全脫鉤：這裡只處理「已經打過18模組
分數、還沒被任何一期選用過」的話題。

選題邏輯分三輪：
1. 模組輪動：18 個模組各自找目前分數最高、還沒入選的話題優先入選，
   確保每個模組群都有機會被照顧到，不會被少數幾個熱門話題把版位吃光
2. 補位：版位還沒滿就用跨模組總分（18個模組分數加總）排序遞補
3. 保底：如果連 total_topics 的下限都不到，才放寬 content_type 配額上限
   硬選，避免開天窗；但不會為了湊數硬選 min_module_score_to_select 以下的
   話題（那代表這期真的沒有適合的內容，比湊數更誠實）

content_type 配額比照 pipeline/pool_selection.py 的 area_quota 寫法：
某類別缺貨就從別類遞補，不會因為某類別掛零就報錯。
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from pipeline.topic_db import get_articles_for_topic, get_available_topics


def compute_hotness(article_rows: list[sqlite3.Row], as_of: datetime, half_life_days: float) -> float:
    """熱門度 = 報導家數（不重複來源數）× 平均來源權重 × 時間衰減。
    時間衰減用半衰期：距離話題最後一次有新文章加入的天數每過一個半衰期，
    熱度打對折。
    """
    if not article_rows:
        return 0.0
    report_count = len({row["source_id"] for row in article_rows})
    avg_weight = sum(row["source_weight"] for row in article_rows) / len(article_rows)
    last_seen = max(datetime.fromisoformat(row["published_at"]) for row in article_rows)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    days_since = max((as_of - last_seen).total_seconds() / 86400, 0.0)
    time_decay = 0.5 ** (days_since / half_life_days)
    return report_count * avg_weight * time_decay


def cross_module_total(module_scores: dict) -> float:
    return sum(entry["score"] for entry in module_scores.values())


def _build_scored_candidates(conn: sqlite3.Connection, config: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    half_life = config["hotness"]["half_life_days"]

    scored = []
    for row in get_available_topics(conn):
        articles = get_articles_for_topic(conn, row["id"])
        module_scores = json.loads(row["module_scores_json"])
        scored.append(
            {
                "row": row,
                "articles": articles,
                "hotness": compute_hotness(articles, now, half_life),
                "module_scores": module_scores,
                "content_type": row["content_type"],
                "cross_module_total": cross_module_total(module_scores),
            }
        )
    return scored


def _all_module_ids(modules_config: dict) -> list[str]:
    return [m["id"] for group in ("functional", "domain") for m in modules_config[group]]


def select_for_issue(conn: sqlite3.Connection, config: dict) -> list[dict]:
    """回傳入選話題清單，每個元素是
    {"row", "articles", "hotness", "module_scores", "content_type", "cross_module_total"}
    """
    scored = _build_scored_candidates(conn, config)

    selection_cfg = config["selection"]
    total_min, total_max = selection_cfg["total_topics"]
    content_type_quota: dict[str, list[int]] = selection_cfg["content_type_quota"]
    per_module_cap = selection_cfg["per_module_cap"]
    min_module_score = selection_cfg["min_module_score_to_select"]
    all_module_ids = _all_module_ids(config["modules"])

    selected: list[dict] = []
    selected_ids: set[int] = set()
    type_counts: Counter[str] = Counter()

    def quota_max(content_type: str) -> int:
        return content_type_quota.get(content_type, [0, total_max])[1]

    def add(entry: dict) -> None:
        selected.append(entry)
        selected_ids.add(entry["row"]["id"])
        type_counts[entry["content_type"]] += 1

    # 第一輪：模組輪動，各模組取目前最高分、還沒入選、分數夠格的話題
    for module_id in all_module_ids:
        if len(selected) >= total_max:
            break
        candidates = sorted(
            (e for e in scored if e["row"]["id"] not in selected_ids),
            key=lambda e: e["module_scores"][module_id]["score"],
            reverse=True,
        )
        picked_for_module = 0
        for entry in candidates:
            if picked_for_module >= per_module_cap or len(selected) >= total_max:
                break
            if entry["module_scores"][module_id]["score"] < min_module_score:
                break  # 排序過的候選清單，這個分數以下的更不用看
            if type_counts[entry["content_type"]] >= quota_max(entry["content_type"]):
                continue
            add(entry)
            picked_for_module += 1

    # 第二輪：跨模組總分排序補滿版位，一樣尊重 content_type 配額上限
    if len(selected) < total_max:
        remaining = sorted(
            (e for e in scored if e["row"]["id"] not in selected_ids),
            key=lambda e: e["cross_module_total"],
            reverse=True,
        )
        for entry in remaining:
            if len(selected) >= total_max:
                break
            if type_counts[entry["content_type"]] >= quota_max(entry["content_type"]):
                continue
            add(entry)

    # 第三輪（保底）：離下限還有距離就放寬配額上限，但不放寬最低分門檻
    if len(selected) < total_min:
        remaining = sorted(
            (e for e in scored if e["row"]["id"] not in selected_ids),
            key=lambda e: e["cross_module_total"],
            reverse=True,
        )
        for entry in remaining:
            if len(selected) >= total_min:
                break
            add(entry)

    selected.sort(key=lambda e: e["cross_module_total"], reverse=True)
    return selected[:total_max]
