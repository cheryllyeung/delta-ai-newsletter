"""出刊前檢查（2026-10-05 加）。

這支把過去每一期都靠人手工做的檢查固定下來。做法是把已渲染的 EDM 與資料庫
一起看，分三個等級回報：

    FAIL  不該寄出去，例如版面區塊缺失、信裡出現內網位址
    WARN  需要人看一眼，例如某個數字在當期原文裡找不到、用詞強度偏強
    OK    這一項沒問題

為什麼要分 WARN 而不是全部當錯：有些檢查天生會誤報（3,300 萬美元對應原文的
$33M，數字字面不同但正確），硬擋會逼人關掉檢查。WARN 的用途是把人的注意力
導到該看的那幾行，不是代替人判斷。

用法：
    python -m tools.preflight_issue                     # 最新一期
    python -m tools.preflight_issue --issue-id 11
    python -m tools.preflight_issue --issue-id 11 --strict   # WARN 也算失敗
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.summary_overlap import THRESHOLD, overlap
from pipeline.topic_db import get_articles_for_topic, get_connection

ROOT = Path(__file__).resolve().parent.parent

# 強度詞：出現時要能在原文找到對應說法，否則就是把來源的保留講成定論
_STRENGTH = {
    "證實": ("confirm", "demonstrat", "prove", "證實", "證明"),
    "證明": ("confirm", "demonstrat", "prove", "證明"),
    "確立": ("establish", "確立"),
    "首次": ("first ", "first-", "首次", "首度"),
    "首度": ("first ", "first-", "首次", "首度"),
    "唯一": ("only ", "sole ", "唯一"),
    "最大": ("largest", "biggest", "最大"),
}

# 版面一定要有的東西
_REQUIRED_MARKERS = ["本期導讀", "主編觀察", "關於這份日報", "看原文"]
_FORBIDDEN_MARKERS = ["TWTP1NB3422", "閱讀全文", "[請填]"]
_FOREIGN = re.compile(r"[Ѐ-ӿ぀-ヿ가-힯]")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append((level, name, detail))

    def count(self, level: str) -> int:
        return sum(1 for lv, _, _ in self.rows if lv == level)

    def show(self) -> None:
        order = {"FAIL": 0, "WARN": 1, "OK": 2}
        for lv, name, detail in sorted(self.rows, key=lambda r: order[r[0]]):
            line = f"  [{lv:4}] {name}"
            if detail:
                line += f"：{detail}"
            print(line)
        print(f"\n  FAIL {self.count('FAIL')}　WARN {self.count('WARN')}　OK {self.count('OK')}")


def _visible_text(raw_html: str) -> str:
    body = re.sub(r"<!--.*?-->", "", raw_html, flags=re.S)
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", body, flags=re.S)
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", body))


def _digits(text: str) -> set[str]:
    return {m.group(0).replace(",", "").rstrip("%") for m in _NUMBER.finditer(text or "")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    parser.add_argument("--html", default=None, help="已渲染的 EDM，預設找 runs/email_preview_<日期>.html")
    parser.add_argument("--strict", action="store_true", help="WARN 也視為失敗")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))
    if args.issue_id:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (args.issue_id,)).fetchone()
    else:
        issue = conn.execute(
            "SELECT * FROM issues ORDER BY issue_date DESC, id DESC LIMIT 1"
        ).fetchone()
    if issue is None:
        print("[preflight] 找不到期數。")
        sys.exit(1)

    html_path = Path(args.html) if args.html else ROOT / "runs" / f"email_preview_{issue['issue_date']}.html"
    if not html_path.exists():
        print(f"[preflight] 找不到渲染結果：{html_path}\n  先跑 python -m tools.render_issue_email --issue-id {issue['id']}")
        sys.exit(1)

    raw = html_path.read_text(encoding="utf-8")
    text = _visible_text(raw)
    rows = conn.execute(
        "SELECT topic_id, generated_json, needs_review, confidence FROM generated_topics "
        "WHERE issue_id = ? ORDER BY id",
        (issue["id"],),
    ).fetchall()
    rep = Report()
    print(f"[preflight] 第 {issue['id']} 期（{issue['issue_date']}），{len(rows)} 則，檢查 {html_path.name}\n")

    # 1. 版面該有的與不該有的
    missing = [m for m in _REQUIRED_MARKERS if m not in raw]
    rep.add("FAIL" if missing else "OK", "版面必要區塊", "缺少 " + "、".join(missing) if missing else "齊全")
    present = [m for m in _FORBIDDEN_MARKERS if m in raw]
    rep.add("FAIL" if present else "OK", "不該出現的內容", "出現 " + "、".join(present) if present else "乾淨")

    links = raw.count("看原文")
    rep.add("FAIL" if links < len(rows) else "OK", "每則都有原文連結", f"{links} 個連結 / {len(rows)} 則")

    # 2. 導讀與下方標題逐字一致
    digest = [x.replace("&nbsp;", " ") for x in re.findall(r'text-indent:-14px;">— (?:<a[^>]*>)?(?:<span[^>]*>)?([^<]+)', raw)]
    heads = {json.loads(r["generated_json"]).get("chosen_headline", "").strip() for r in rows}
    mismatch = [d for d in digest if d.split("　", 1)[-1].strip() not in heads]
    rep.add("FAIL" if mismatch else "OK", "導讀與報導標題一致",
            f"{len(mismatch)} 行對不上：{mismatch[:2]}" if mismatch else f"{len(digest)} 行全部一致")

    # 3. 繁體與外語殘留
    foreign = _FOREIGN.findall(text)
    rep.add("FAIL" if foreign else "OK", "無外語字元殘留", f"{len(foreign)} 處" if foreign else "乾淨")

    # 4. 標題與摘要重複度
    worst = []
    for r in rows:
        g = json.loads(r["generated_json"])
        score = overlap(g.get("chosen_headline", ""), (g.get("card_summary") or {}).get("text", ""))
        if score > THRESHOLD:
            worst.append(f"{score:.0%} {g.get('chosen_headline', '')[:24]}")
    rep.add("WARN" if worst else "OK", "摘要沒有重述標題",
            "；".join(worst) if worst else f"全部低於 {THRESHOLD:.0%}")

    # 5. 強度用詞要有原文依據
    pool = "\n".join(
        f"{a['title']}\n{a['content']}"
        for r in rows
        for a in get_articles_for_topic(conn, r["topic_id"])
    )
    flagged = []
    for word, markers in _STRENGTH.items():
        for r in rows:
            blob = json.dumps(json.loads(r["generated_json"]), ensure_ascii=False)
            if word in blob and not any(m.lower() in pool.lower() for m in markers):
                flagged.append(f"{word}（原文找不到對應說法）")
                break
    rep.add("WARN" if flagged else "OK", "強度用詞有原文依據",
            "；".join(sorted(set(flagged))) if flagged else "無過強用詞")

    # 6. 主編觀察的數字要來自當期原文
    tldr = json.loads(issue["tldr_json"]) if issue["tldr_json"] else {}
    pool_digits = _digits(pool)
    def _traceable(num: str) -> bool:
        """這個數字在原文裡找不找得到。中文的萬與億要換算過再比：原文寫
        $33M，我們寫 3,300 萬美元，字面不同但是同一個數（2026-10-05 誤報過）。"""
        if num in pool_digits:
            return True
        try:
            value = float(num)
        except ValueError:
            return True
        # 1e-2 / 1e2 是「萬」與「million」的差（3,300 萬 = 33 M），
        # 1e-4 / 1e4 是「萬」與個位，1e-8 / 1e8 是「億」，1e3 / 1e6 給 K 與 M。
        for scale in (1e-2, 1e2, 1e4, 1e8, 1e-4, 1e-8, 1e3, 1e6):
            scaled = value * scale
            if scaled.is_integer() and str(int(scaled)) in pool_digits:
                return True
        return False

    unknown = []
    for sec in tldr.get("editorial_sections") or []:
        for key in ("lead", "body", "question"):
            for num in _digits(sec.get(key) or ""):
                if len(num) > 1 and not _traceable(num):
                    unknown.append(f"{sec.get('dimension')}:{num}")
    rep.add("WARN" if unknown else "OK", "主編觀察數字可回溯",
            "；".join(unknown) if unknown else "每個數字都在原文找得到")

    # 7. 自檢信心
    low = [
        json.loads(r["generated_json"]).get("chosen_headline", "")[:24]
        for r in rows
        if r["needs_review"]
    ]
    rep.add("WARN" if low else "OK", "自檢信心", f"{len(low)} 則偏低：{low}" if low else "全部通過")

    # 8. 預印本與日期標示
    undated = len(rows) - len(re.findall(r"20\d\d-\d\d-\d\d", raw))
    rep.add("WARN" if undated > 0 else "OK", "每則標了發布日期",
            f"少 {undated} 則" if undated > 0 else "齊全")

    rep.show()
    fail = rep.count("FAIL") or (args.strict and rep.count("WARN"))
    print("\n[preflight] " + ("有項目需要處理，先別寄。" if fail else "檢查通過，可以寄。"))
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
