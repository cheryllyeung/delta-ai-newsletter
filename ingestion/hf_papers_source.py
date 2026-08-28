"""從 Hugging Face Daily Papers 抓每日熱門 arXiv 論文。

arXiv 官方 API 沒有流量或熱門排序（ingestion/arxiv_source.py 只能照投稿
時間抓最新），這裡用 HF Daily Papers 當「熱門論文」的代理：每天由社群
投票挑出的 arXiv 論文，公開 API 不需金鑰，upvote 數直接當 score。
2026-08-28 加（使用者需求：arXiv 要能抓到當天最熱門的，不只最新的）。

API：GET https://huggingface.co/api/daily_papers?limit=N
每筆的 paper.id 就是 arXiv id，url 統一指回 arxiv.org 的摘要頁，這樣同一篇
論文從 arxiv_ai（最新投稿）跟這裡（熱門）兩條路進來會在入池時被 URL 去重。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ingestion.base import RawItem

HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers"


def fetch_hf_daily_papers(
    source_id: str,
    source_name: str,
    weight: float,
    limit: int = 20,
    days_back: int = 14,
    min_upvotes: int = 0,
    timeout: int = 20,
    date: str | None = None,
) -> list[RawItem]:
    """回傳近 days_back 天內、upvote 數達門檻的每日熱門論文。

    date（YYYY-MM-DD）有給就抓那一天的榜單（tools/backfill_hf_papers.py
    回補歷史用），沒給就是最新的。"""
    params: dict = {"limit": limit}
    if date:
        params["date"] = date
    response = requests.get(
        HF_DAILY_PAPERS_URL,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    response.raise_for_status()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[RawItem] = []
    for entry in response.json():
        try:
            paper = entry["paper"]
            upvotes = int(paper.get("upvotes") or 0)
            if upvotes < min_upvotes:
                continue
            # 用「上榜日」不用論文投稿日：這個來源的意義是「今天社群在看什麼」，
            # 論文本身可能是幾天前投的。
            published_at = datetime.fromisoformat(
                (entry.get("publishedAt") or paper["publishedAt"]).replace("Z", "+00:00")
            )
            if published_at < cutoff:
                continue
            items.append(
                RawItem(
                    title=(paper.get("title") or "").replace("\n", " ").strip(),
                    url=f"https://arxiv.org/abs/{paper['id']}",
                    source="hf_papers",
                    subdomain_id=source_id,
                    published_at=published_at,
                    summary=(paper.get("summary") or "").replace("\n", " ").strip(),
                    score=float(upvotes),
                    extra={
                        "source_name": source_name,
                        "source_weight": weight,
                        "hf_upvotes": upvotes,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 -- 單筆格式異常不該拖垮整批
            print(f"[hf_papers_source]   entry 解析失敗，跳過：{exc}")
            continue
    return items
