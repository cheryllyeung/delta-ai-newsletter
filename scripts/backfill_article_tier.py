"""一次性回填：圖上線之前入池的舊文章沒有 tier 欄位（那時還沒加這個
欄位），用 config/topics.yaml 目前的 source name → tier 對照表補回去，
避免圖重建時歷史文章缺 tier、只有新資料有分層這種斷層。

只補 tier IS NULL 的列，不覆蓋已經有值的（見
pipeline/topic_db.py::backfill_article_tier() 的說明）。

用法：
    python -m scripts.backfill_article_tier
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from pipeline.topic_db import backfill_article_tier, get_connection

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    conn = get_connection(config["database"]["path"])

    source_name_to_tier = {
        source["name"]: source.get("tier", "case") for source in config["sources"]
    }
    updated = backfill_article_tier(conn, source_name_to_tier)
    print(f"[backfill_article_tier] 回填完成，更新 {updated} 篇文章的 tier 欄位。")


if __name__ == "__main__":
    main()

