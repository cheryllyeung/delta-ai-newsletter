from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawItem:
    """一筆從任一來源抓到的原始內容，尚未經過分類/摘要。"""

    title: str
    url: str
    source: str  # e.g. "arxiv" / "reddit"
    subdomain_id: str  # config/domains.yaml 中的 subdomain id，或 "general"
    published_at: datetime
    summary: str = ""  # arXiv abstract / reddit selftext 或 top comment 摘要
    score: float = 0.0  # 熱門程度分數（reddit upvotes、或 arxiv 新鮮度權重），用於排序
    extra: dict = field(default_factory=dict)  # 來源特定的額外資訊（如 reddit num_comments）

    def dedupe_key(self) -> str:
        return self.url.strip().rstrip("/").lower()
