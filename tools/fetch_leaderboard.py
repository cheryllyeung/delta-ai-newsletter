"""抓 LMArena 各分類排行榜快照，存進 SQLite 給發佈頁（/releases）顯示。

2026-08-28 改版：原本只抓一份總榜，而且寫的當天外網掛掉、兩個端點都沒
實測過（lmarena.ai/api/leaderboard 實測回 403，從來沒成功過）。使用者要
的是「各個功能（文字、程式、視覺、圖像生成）哪個模型排最前面」，改成
抓 lmarena.ai 的分類榜。

lmarena.ai 沒有公開 API，但每個分類頁的 HTML 內嵌完整榜單（Next.js
flight payload 裡的 \"leaderboard\":{...\"entries\":[...]}，含 rank／
rating／votes／開發者），直接把那段 JSON 挖出來。頁面改版欄位變了會解析
失敗：抓不到的分類就不寫入，頁面繼續顯示上一份快照，不會開天窗。

每個分類存成獨立 source（lmarena-text、lmarena-webdev…），升降照舊由
顯示端拿最近兩份快照比。輕量設計不變：只存當期快照，不存歷史走勢。

用法：
    python -m tools.fetch_leaderboard            # 全部分類各抓一次
    python -m tools.fetch_leaderboard --dry-run  # 抓但不寫入，各印前 5 名
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.topic_db import get_connection, save_leaderboard_snapshot

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"

_TIMEOUT = 30
_TOP_N = 15  # 每個分類留前 15 名，夠頁面顯示
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# (存進 DB 的 key, lmarena.ai/leaderboard/ 底下的路徑, 顯示名)。
# 程式開發用 text 榜的 coding 子分類：webdev arena 的榜單是前端載入的、
# 頁面沒內嵌資料（實測只有殘缺的 rankByModality，缺前段名次還有重複），
# text/coding 有完整的內嵌榜單跟分數。
CATEGORIES = [
    ("text", "text", "文字對話"),
    ("coding", "text/coding", "程式開發"),
    ("vision", "vision", "視覺理解"),
    ("text-to-image", "text-to-image", "圖像生成"),
    ("search", "search", "搜尋"),
]


def _extract_entries(html: str) -> list[dict]:
    """從分類頁 HTML 挖出第一份 leaderboard 的 entries 陣列。

    資料在 <script>self.__next_f.push([1,"..."]) 的 JS 字串裡，引號都是
    跳脫過的（\\"）。先定位 \\"entries\\":[ ，用中括號深度走到對應的 ]，
    再把這段當 JS 字串解跳脫、最後當 JSON 解析。
    """
    anchor = '\\"leaderboard\\":{'
    i = html.find(anchor)
    if i == -1:
        raise ValueError("頁面裡找不到 leaderboard 資料（改版了？）")
    j = html.find('\\"entries\\":[', i)
    if j == -1:
        raise ValueError("leaderboard 裡找不到 entries（改版了？）")
    start = html.index("[", j)
    depth = 0
    end = None
    for pos in range(start, min(start + 2_000_000, len(html))):
        ch = html[pos]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = pos
                break
    if end is None:
        raise ValueError("entries 陣列沒有閉合")
    unescaped = json.loads(f'"{html[start : end + 1]}"')  # JS 字串解跳脫
    return json.loads(unescaped)


def fetch_category(path: str) -> list[dict]:
    resp = requests.get(f"https://lmarena.ai/leaderboard/{path}", headers=_UA, timeout=_TIMEOUT)
    resp.raise_for_status()
    entries = _extract_entries(resp.text)
    rows = []
    for e in entries:
        model = e.get("modelDisplayName") or e.get("modelKey")
        if not model or e.get("rank") is None:
            continue
        rating = e.get("rating")
        rows.append(
            {
                "rank": e.get("rank"),
                "model": str(model),
                "org": e.get("modelOrganization"),
                "score": round(float(rating), 1) if rating is not None else None,
                "votes": e.get("votes"),
            }
        )
    if not rows:
        raise ValueError("解析不出任何一列（欄位名可能又改了）")
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"]))
    return rows[:_TOP_N]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="抓但不寫入，各印前 5 名")
    args = parser.parse_args()

    conn = None
    if not args.dry_run:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        conn = get_connection(config["database"]["path"])

    ok = 0
    for key, path, title in CATEGORIES:
        try:
            rows = fetch_category(path)
        except Exception as exc:  # noqa: BLE001 -- 單一分類失敗不影響其他分類
            print(f"[fetch_leaderboard] {title}（{path}）失敗：{exc}")
            continue
        ok += 1
        print(f"[fetch_leaderboard] {title}：{len(rows)} 名")
        for row in rows[:5]:
            score_text = row["score"] if row["score"] is not None else "—"
            print(f"  {row['rank']:>2}. {row['model']}（{row.get('org') or '?'}）{score_text}")
        if conn is not None:
            save_leaderboard_snapshot(conn, f"lmarena-{key}", rows)
        time.sleep(1)  # 對站方客氣一點

    if ok == 0:
        print("[fetch_leaderboard] 全部分類都失敗，這次不寫入（頁面繼續用上一份快照）。")
        sys.exit(1)
    if args.dry_run:
        print("[fetch_leaderboard] --dry-run：沒有寫入。")
    else:
        print(f"[fetch_leaderboard] 完成，寫入 {ok} 個分類的快照。")


if __name__ == "__main__":
    main()
