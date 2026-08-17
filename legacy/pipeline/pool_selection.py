"""每次要出刊時，對長期文章池下一次查詢，決定這期要選哪些文章。

跟評分（pipeline/case_scoring.py）完全脫鉤：這裡只處理「已經評分過、
還沒被任何一期選用過」的文章，依 area_category 配額 + 趨勢加成排序選文。
某分類供給不足時從別的分類遞補，不會為了湊配額硬選低分文章，也不會
因為某分類掛零而報錯。
"""
from __future__ import annotations

import sqlite3

from pipeline.pool_db import get_available_pool, get_tags_for_article
from pipeline.trend import compute_trend_scores, normalize_trend_boost, trend_boost_for_tags


def select_for_issue(conn: sqlite3.Connection, config: dict) -> list[dict]:
    """回傳入選文章清單，每個元素是
    {"row": sqlite3.Row（articles表的一列）, "tags": list[str], "final_score": float}
    """
    pool = get_available_pool(conn)
    trend_cfg = config["trend"]
    trend_scores = compute_trend_scores(
        conn, recent_days=trend_cfg["recent_days"], baseline_days=trend_cfg["baseline_days"]
    )
    trend_weight = trend_cfg["trend_weight"]

    scored = []
    for row in pool:
        tags = get_tags_for_article(conn, row["id"])
        boost = normalize_trend_boost(trend_boost_for_tags(tags, trend_scores))
        final_score = row["base_score"] * (1 + trend_weight * boost)
        scored.append({"row": row, "tags": tags, "final_score": final_score})

    area_quota: dict[str, list[int]] = config["selection"]["area_quota"]
    total_min, total_max = config["selection"]["total_cases"]

    selected: list[dict] = []
    for area, (_qmin, qmax) in area_quota.items():
        bucket = sorted(
            (s for s in scored if s["row"]["area_category"] == area),
            key=lambda s: s["final_score"],
            reverse=True,
        )
        selected.extend(bucket[:qmax])

    selected_ids = {s["row"]["id"] for s in selected}
    if len(selected) < total_min:
        leftover = sorted(
            (s for s in scored if s["row"]["id"] not in selected_ids),
            key=lambda s: s["final_score"],
            reverse=True,
        )
        selected.extend(leftover[: total_min - len(selected)])

    selected.sort(key=lambda s: s["final_score"], reverse=True)
    return selected[:total_max]
