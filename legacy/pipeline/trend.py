"""從文章池的 hashtag 分佈算「這個詞最近是不是正在變成趨勢」。

方法很簡單（POC 階段刻意不做複雜的時間序列模型）：比較每個 tag 在「近期
窗口」跟「基期窗口」的出現密度，密度比越高、且近期出現次數本身越多，
代表這個詞最近密集出現，trend_score 就越高。之後要調準度，優先從這支
模組的公式下手，不用動其他選文邏輯。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

from pipeline.pool_db import get_tag_counts_since

_SMOOTHING = 0.01


def compute_trend_scores(
    conn: sqlite3.Connection, recent_days: int = 14, baseline_days: int = 60
) -> dict[str, float]:
    """回傳 {tag: trend_score}。分數沒有固定上限，數值越大代表越像趨勢，
    在 trend_boost_for_tags() / normalize_trend_boost() 那一步才壓縮到可用範圍。
    """
    now = datetime.now()
    recent_counts = get_tag_counts_since(conn, (now - timedelta(days=recent_days)).isoformat())
    baseline_counts = get_tag_counts_since(conn, (now - timedelta(days=baseline_days)).isoformat())

    trend_scores: dict[str, float] = {}
    for tag, recent_n in recent_counts.items():
        recent_rate = recent_n / recent_days
        baseline_rate = baseline_counts.get(tag, 0) / baseline_days
        concentration = recent_rate / (baseline_rate + _SMOOTHING)
        trend_scores[tag] = recent_n * concentration
    return trend_scores


def trend_boost_for_tags(tags: list[str], trend_scores: dict[str, float]) -> float:
    """一篇文章的趨勢加成＝它所有 hashtag 裡最高的 trend_score，沒有標籤就是 0。"""
    if not tags:
        return 0.0
    return max(trend_scores.get(tag, 0.0) for tag in tags)


def normalize_trend_boost(raw_boost: float) -> float:
    """把量級不固定的 trend_score 壓縮到約 0-1，避免單一冷門詞的極端值把
    最終排序分數炸開。用 tanh 做簡單的軟壓縮。
    """
    return math.tanh(raw_boost / 10.0)
