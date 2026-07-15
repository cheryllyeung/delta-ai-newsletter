from __future__ import annotations

from ingestion.base import RawItem


def dedupe(items: list[RawItem]) -> list[RawItem]:
    """依網址去重，同網址只保留分數較高的那筆。"""
    best: dict[str, RawItem] = {}
    for item in items:
        key = item.dedupe_key()
        current = best.get(key)
        if current is None or item.score > current.score:
            best[key] = item
    return list(best.values())


def top_n_per_subdomain(items: list[RawItem], n: int) -> list[RawItem]:
    """每個 subdomain 依分數排序後只保留前 n 筆。"""
    by_subdomain: dict[str, list[RawItem]] = {}
    for item in items:
        by_subdomain.setdefault(item.subdomain_id, []).append(item)

    result: list[RawItem] = []
    for subdomain_items in by_subdomain.values():
        subdomain_items.sort(key=lambda i: i.score, reverse=True)
        result.extend(subdomain_items[:n])
    return result
