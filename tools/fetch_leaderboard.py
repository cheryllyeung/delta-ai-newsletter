"""抓 LMArena 模型排行榜快照，存進 SQLite 給發佈頁（/releases）顯示。

輕量版（2026-08-28）：只存「當期快照」，升降是顯示時拿最近兩份快照比出來
的，不存歷史走勢。每天跑一次就有昨天跟今天可以比；抓失敗就什麼都不寫，
頁面繼續顯示上一份快照，不會開天窗。

資料來源按順序嘗試，第一個成功的就用：
1. LMArena 官方 API（lmarena.ai）
2. Hugging Face space（lmarena-ai/chatbot-arena-leaderboard）裡的
   leaderboard_table CSV，檔名帶日期，抓最新一份

注意：寫這支的當天外網 DNS 剛好掛掉，兩個端點都沒實測過。第一次跑如果
兩個都失敗，開瀏覽器看一下現在的資料長怎樣再回來調 _CANDIDATES。

用法：
    python -m tools.fetch_leaderboard            # 抓一次，存快照
    python -m tools.fetch_leaderboard --dry-run  # 抓但不寫入，印前 10 名
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.topic_db import get_connection, save_leaderboard_snapshot

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"

_TIMEOUT = 30
_TOP_N = 30  # 快照只留前 30 名，夠頁面顯示，不用整份幾百列都存


def _fetch_lmarena_api() -> list[dict]:
    """LMArena 官方端點。回傳格式沒有文件，抓到什麼就寬鬆解析什麼。"""
    resp = requests.get("https://lmarena.ai/api/leaderboard", timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    # 常見包法：直接是 list，或包在 data/models/leaderboard 這類鍵底下
    if isinstance(data, dict):
        for key in ("data", "models", "leaderboard", "entries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        raise ValueError("回應不是名次列表")
    return _normalise_rows(data)


def _fetch_hf_space_csv() -> list[dict]:
    """HF space 裡的 leaderboard_table_<日期>.csv，檔名取字典序最大的就是最新。"""
    tree = requests.get(
        "https://huggingface.co/api/spaces/lmarena-ai/chatbot-arena-leaderboard/tree/main",
        timeout=_TIMEOUT,
    )
    tree.raise_for_status()
    names = [
        entry["path"]
        for entry in tree.json()
        if entry.get("path", "").startswith("leaderboard_table_") and entry["path"].endswith(".csv")
    ]
    if not names:
        raise ValueError("space 裡找不到 leaderboard_table_*.csv")
    latest = sorted(names)[-1]
    resp = requests.get(
        f"https://huggingface.co/spaces/lmarena-ai/chatbot-arena-leaderboard/resolve/main/{latest}",
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    if not rows:
        raise ValueError(f"{latest} 是空的")
    return _normalise_rows(rows)


def _pick(row: dict, *keywords: str):
    """從一列資料裡挑出欄位名（不分大小寫）含任一關鍵字的值。
    兩個來源的欄位名都不受我們控制，用關鍵字對而不是寫死名字。"""
    for key, value in row.items():
        lowered = str(key).lower()
        if any(kw in lowered for kw in keywords):
            return value
    return None


def _normalise_rows(raw_rows: list[dict]) -> list[dict]:
    """把來源各自的欄位名整理成固定的 rank/model/org/score，依分數新排名次。"""
    rows = []
    for raw in raw_rows:
        model = _pick(raw, "model", "name")
        score = _pick(raw, "elo", "rating", "score", "arena")
        if model is None or score is None:
            continue
        try:
            score = round(float(score), 1)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "model": str(model),
                "org": _pick(raw, "organization", "org", "developer"),
                "score": score,
            }
        )
    if not rows:
        raise ValueError("解析不出任何一列（欄位名可能又改了）")
    rows.sort(key=lambda r: -r["score"])
    return [{"rank": i, **row} for i, row in enumerate(rows[:_TOP_N], start=1)]


_CANDIDATES = [
    ("lmarena.ai API", _fetch_lmarena_api),
    ("HF space CSV", _fetch_hf_space_csv),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="抓但不寫入，印前 10 名")
    args = parser.parse_args()

    rows = None
    for label, fetch in _CANDIDATES:
        try:
            rows = fetch()
            print(f"[fetch_leaderboard] 用「{label}」抓到 {len(rows)} 名。")
            break
        except Exception as exc:  # noqa: BLE001 -- 換下一個來源試，全失敗才放棄
            print(f"[fetch_leaderboard] {label} 失敗：{exc}")

    if rows is None:
        print("[fetch_leaderboard] 全部來源都失敗，這次不寫入（頁面會繼續用上一份快照）。")
        sys.exit(1)

    for row in rows[:10]:
        print(f"  {row['rank']:>2}. {row['model']}（{row.get('org') or '?'}）{row['score']}")

    if args.dry_run:
        print("[fetch_leaderboard] --dry-run：沒有寫入。")
        return

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    conn = get_connection(config["database"]["path"])
    save_leaderboard_snapshot(conn, "lmarena", rows)
    print(f"[fetch_leaderboard] 已存成快照（source=lmarena，共 {len(rows)} 名）。")


if __name__ == "__main__":
    main()
