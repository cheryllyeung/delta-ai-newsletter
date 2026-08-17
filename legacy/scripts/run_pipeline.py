"""端到端執行：讀取領域設定 -> 抓取來源 -> 去重/篩選 -> LLM生成 -> 渲染成HTML草稿。

MVP 階段刻意不含自動發送：產出的 HTML 草稿存在 output/drafts/，
交由人工審核後再手動發送（詳見 README 路線圖）。

用法：
    python -m scripts.run_pipeline
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from generation.summarize import summarize_subdomain
from ingestion.arxiv_source import fetch_arxiv_items
from ingestion.reddit_source import fetch_reddit_items
from pipeline.dedupe import dedupe, top_n_per_subdomain
from render.build_newsletter import render_newsletter, save_draft

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "domains.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def collect_items_for_subdomain(subdomain: dict) -> list:
    items = []
    items += fetch_arxiv_items(
        subdomain_id=subdomain["id"],
        categories=subdomain.get("arxiv_categories", []),
        keywords=subdomain.get("arxiv_keywords", []),
    )
    items += fetch_reddit_items(
        subdomain_id=subdomain["id"],
        subreddits=subdomain.get("subreddits", []),
    )
    return items


def main() -> None:
    config = load_config()
    max_items = config.get("max_items_per_subdomain", 3)

    rendered_groups = []
    for group in config["groups"]:
        rendered_subdomains = []
        for subdomain in group["subdomains"]:
            print(f"[run_pipeline] 收集 {group['name']} - {subdomain['name']} ...")
            raw_items = collect_items_for_subdomain(subdomain)
            raw_items = dedupe(raw_items)
            raw_items = top_n_per_subdomain(raw_items, max_items)

            drafted_items = summarize_subdomain(
                domain_name=group["name"],
                subdomain_name=subdomain["name"],
                subdomain_description=subdomain.get("description", ""),
                items=raw_items,
            )
            rendered_subdomains.append({"name": subdomain["name"], "entries": drafted_items})
        rendered_groups.append({"name": group["name"], "subdomains": rendered_subdomains})

    general_cfg = config.get("general_ai_trends", {})
    print("[run_pipeline] 收集 AI 通識趨勢 ...")
    general_raw = fetch_arxiv_items(
        subdomain_id="general",
        categories=general_cfg.get("arxiv_categories", []),
        keywords=[],
    )
    general_raw += fetch_reddit_items(
        subdomain_id="general",
        subreddits=general_cfg.get("subreddits", []),
    )
    general_raw = top_n_per_subdomain(dedupe(general_raw), max_items)
    general_items = summarize_subdomain(
        domain_name="AI 通識趨勢",
        subdomain_name="",
        subdomain_description="跨領域的重大 AI 新聞、新模型發布、產業動態",
        items=general_raw,
    )

    html = render_newsletter(groups=rendered_groups, general_items=general_items)
    out_path = save_draft(html)
    print(f"[run_pipeline] 草稿已產出：{out_path}")


if __name__ == "__main__":
    main()
