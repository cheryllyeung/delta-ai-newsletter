"""標題與摘要的重複度（2026-10-05 加）。

為什麼要量這個：使用者反映「下方文章標題跟摘要重複性太高」，實測第 11 期
八則的平均重疊率 54%，最高兩則 84% 與 78%，正好就是他被刺到的那兩則。
所以這個指標跟人的感受一致，可以拿來當自動把關，不必每次靠人讀。

作法是字元 2-gram 的集合重疊，分母用標題：摘要把標題講完一遍就會接近 1，
摘要補的是標題沒有的東西就會低。中文沒有詞邊界，2-gram 比切詞穩，也不必
載詞典。
"""
from __future__ import annotations

import re

# 標點與空白不算內容，避免「，」「的」這類符號把分數撐高
_STRIP = re.compile(r"[\s，。、：；！？（）()「」『』【】\-－—…·,.:;!?\"']")

# 超過這個就算重複過頭，要重寫（實測 0.55 大致對應「讀起來像同一句話」）
THRESHOLD = 0.55


def _grams(text: str) -> set[str]:
    flat = _STRIP.sub("", text or "")
    return {flat[i : i + 2] for i in range(len(flat) - 1)}


def overlap(headline: str, summary: str) -> float:
    """摘要蓋掉標題多少內容（0 到 1，以標題為分母）。"""
    head = _grams(headline)
    if not head:
        return 0.0
    return len(head & _grams(summary)) / len(head)


def too_similar(headline: str, summary: str, threshold: float = THRESHOLD) -> bool:
    return overlap(headline, summary) > threshold
