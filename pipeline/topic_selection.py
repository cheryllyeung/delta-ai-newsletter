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


def _module_group_map(modules_config: dict) -> dict[str, str]:
    return {m["id"]: group for group in ("functional", "domain") for m in modules_config[group]}


def _dominant_group(module_scores: dict, group_map: dict[str, str]) -> str:
    """一個話題沒有單一所屬模組（18 個模組各打一次分），補位輪次要判斷
    「這個話題比較像本業還是像通用職能」時，用分數最高的那個模組代表。
    """
    top_module_id = max(module_scores, key=lambda mid: module_scores[mid]["score"])
    return group_map[top_module_id]


def _build_scored_candidates(conn: sqlite3.Connection, config: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    half_life = config["hotness"]["half_life_days"]
    group_map = _module_group_map(config["modules"])

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
                "dominant_group": _dominant_group(module_scores, group_map),
            }
        )
    return scored


def select_for_issue(conn: sqlite3.Connection, config: dict) -> list[dict]:
    """回傳入選話題清單，每個元素是
    {"row", "articles", "hotness", "module_scores", "content_type", "cross_module_total"}

    模組分兩群：domain（能源電力／樓宇自動化／電動車／網通／製造廠務／
    消費性產品／軟體平台／永續節能，台達本業）跟 functional（法務、財會、
    人資⋯泛用職能，任何公司都適用）。domain 只有 8 個、functional 有 10
    個，若不分群直接照模組清單順序輪動，functional 會先把版位挑光，
    整期內容變得像通用生產力工具介紹，跟本業無關。這裡用
    selection.module_group_quota 讓 domain 保底過半版位。
    """
    scored = _build_scored_candidates(conn, config)

    selection_cfg = config["selection"]
    total_min, total_max = selection_cfg["total_topics"]
    content_type_quota: dict[str, list[int]] = selection_cfg["content_type_quota"]
    module_group_quota: dict[str, list[int]] = selection_cfg["module_group_quota"]
    per_module_cap = selection_cfg["per_module_cap"]
    min_module_score = selection_cfg["min_module_score_to_select"]
    group_module_ids = {group: [m["id"] for m in config["modules"][group]] for group in ("domain", "functional")}

    selected: list[dict] = []
    selected_ids: set[int] = set()
    type_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()

    def quota_max(content_type: str) -> int:
        return content_type_quota.get(content_type, [0, total_max])[1]

    def group_quota_max(group: str) -> int:
        return module_group_quota.get(group, [0, total_max])[1]

    def add(entry: dict) -> None:
        selected.append(entry)
        selected_ids.add(entry["row"]["id"])
        type_counts[entry["content_type"]] += 1
        group_counts[entry["dominant_group"]] += 1

    # 第一輪：模組輪動，domain 群先挑（保底過半），functional 群後補；
    # 各模組取目前最高分、還沒入選、分數夠格的話題
    for group in ("domain", "functional"):
        for module_id in group_module_ids[group]:
            if len(selected) >= total_max or group_counts[group] >= group_quota_max(group):
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
                if group_counts[group] >= group_quota_max(group):
                    break
                if entry["module_scores"][module_id]["score"] < min_module_score:
                    break  # 排序過的候選清單，這個分數以下的更不用看
                if type_counts[entry["content_type"]] >= quota_max(entry["content_type"]):
                    continue
                add(entry)
                picked_for_module += 1

    # 第二輪：跨模組總分排序補滿版位，一樣尊重 content_type／module_group 配額上限
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
            if group_counts[entry["dominant_group"]] >= group_quota_max(entry["dominant_group"]):
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
