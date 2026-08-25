"""話題式週報：對話題池下一次查詢，組成一期。

跟抓取/聚類/打分（scripts/ingest_topics.py）完全脫鉤：這支只處理「已經
打過18模組分數、還沒被選用過」的話題，不會自己去抓新資料。

每一個候選話題的去向（入選、或落選的理由與數值）都會寫進 selection_trace
表，出刊後在網頁的選題帳頁面看得到，見 pipeline/gates.py 的說明。

用法：
    python -m scripts.compose_topic_issue --cadence weekly             # 週報：涵蓋上一個完整的週一到週日
    python -m scripts.compose_topic_issue --date 2026-08-01 --cadence daily

週報（2026-08-25 改）：候選池是整週全池、含已上過日報的話題（週報是
「整週最好的回顧」，不是剩菜彙整），排序用週度潛力分（模組分＋實體熱度＋
落選帳，見 pipeline/topic_selection.py），已上過日報的直接重用那篇文章
不重寫。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from generation.topic_generate import OPENING_TECHNIQUES, generate_topic_article
from pipeline import gates
from pipeline.retrieval import retrieve_sources_for_topic
from pipeline.topic_db import (
    attach_trace_to_issue,
    create_issue,
    get_connection,
    get_latest_generated_for_topics,
    mark_topics_published,
    record_selection_trace,
    save_generated_topic,
)
from pipeline.topic_selection import select_for_issue
from pipeline.translate import pretranslate_issue
from review.topic_selfcheck import is_coherent, self_check

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def generate_and_check(conn, selected: list[dict], config: dict) -> tuple[list[dict], list[dict]]:
    """回傳 (成功產出的文章, 生成階段的落選紀錄)。

    生成階段有三種落選：檢索不到素材、重寫後仍然不連貫、生成本身出錯。
    這三種在這支之前都只印一行 log 就算了，話題留在池裡等下次重選，但沒有
    任何地方記得「它今天被試過而且失敗了」。
    """
    newsletter_name = config["newsletter"]["name"]
    quality_cfg = config["quality"]
    # 重寫門檻與人工複審門檻是兩個獨立的值，理由見 config/topics.yaml。
    # 舊設定只有 confidence_threshold 一個值，這裡保留回退相容。
    regenerate_below = quality_cfg.get("regenerate_below", quality_cfg.get("confidence_threshold", 0.6))
    needs_review_below = quality_cfg.get("needs_review_below", quality_cfg.get("confidence_threshold", 0.8))
    max_retries = quality_cfg["max_regeneration_retries"]

    # 開頭手法洗牌後輪流指派給每個入選話題，同一期裡儘量不重複（見
    # generation/topic_generate.py 的 OPENING_TECHNIQUES 說明）。
    shuffled_techniques = OPENING_TECHNIQUES.copy()
    random.shuffle(shuffled_techniques)

    results: list[dict] = []
    rejections: list[dict] = []
    for i, entry in enumerate(selected):
        topic_row = entry["row"]
        topic_id = topic_row["id"]
        content_type = entry["content_type"]
        opening_technique = shuffled_techniques[i % len(shuffled_techniques)]
        print(f"[compose_topic_issue] 檢索素材：{topic_row['representative_title'][:50]} ...")

        try:
            source_rows, source_detail = retrieve_sources_for_topic(conn, config, topic_row)
            if not source_rows:
                # Gate 2 已經擋掉「底下沒有實質文章」的話題，走到這裡通常是
                # 補充素材開著但全部低於門檻。沒有素材就不寫，這比讓模型
                # 從一行標題生出一篇要誠實。
                print("[compose_topic_issue]   沒有可用素材，這篇跳過")
                rejections.append(
                    {
                        "topic_id": topic_id,
                        "decision": "rejected",
                        "reason": "insufficient_sources",
                        "stage": "generation",
                        "detail": source_detail,
                    }
                )
                continue

            article = generate_topic_article(
                newsletter_name, topic_row["representative_title"], content_type, source_rows,
                opening_technique=opening_technique,
            )

            revision_instructions = None
            check_result = None
            for attempt in range(max_retries + 1):
                check_result = self_check(article, source_rows)
                if check_result["confidence"] >= regenerate_below:
                    break
                revision_instructions = check_result.get("revision_instructions", "")
                if attempt < max_retries:
                    print(
                        f"[compose_topic_issue]   自檢信心度 {check_result['confidence']:.2f} "
                        f"< {regenerate_below}，回灌重新生成（第 {attempt + 1} 次重試）"
                    )
                    article = generate_topic_article(
                        newsletter_name, topic_row["representative_title"], content_type, source_rows,
                        revision_instructions,
                        opening_technique=opening_technique,
                    )
        except Exception as exc:  # noqa: BLE001 -- 單篇失敗不中斷整批，這個話題留在池裡下次重跑會重新入選
            print(f"[compose_topic_issue]   這篇生成失敗，跳過（下次重跑會重新入選）：{exc}")
            rejections.append(
                {
                    "topic_id": topic_id,
                    "decision": "rejected",
                    "reason": "generation_error",
                    "stage": "generation",
                    "detail": {"error": str(exc)[:300]},
                }
            )
            continue

        # 連貫性是硬性條件，不是扣分項：標題跟內文對不上、或整篇是兩三件事
        # 硬接在一起，重寫兩次還是這樣就不出刊。這正是同仁反映「看不懂」的
        # 那種文章，寧可少一篇也不要放它出去（見 review/topic_selfcheck.py）。
        if not is_coherent(check_result):
            coherence = check_result.get("coherence_check", {})
            print(f"[compose_topic_issue]   重寫後仍不連貫，這篇不出刊：{coherence.get('note', '')[:80]}")
            rejections.append(
                {
                    "topic_id": topic_id,
                    "decision": "rejected",
                    "reason": "failed_selfcheck",
                    "stage": "generation",
                    "detail": {
                        "confidence": round(check_result["confidence"], 2),
                        "coherence_check": coherence,
                        "sources": source_detail,
                    },
                }
            )
            continue

        needs_review = check_result["confidence"] < needs_review_below
        results.append(
            {
                "topic_id": topic_id,
                "row": topic_row,
                "article": article,
                "source_article_ids": [row["id"] for row in source_rows],
                "confidence": check_result["confidence"],
                "needs_review": needs_review,
                "selected_via": entry.get("selected_via"),
                "source_detail": source_detail,
                "single_source": entry.get("single_source"),
            }
        )
    return results, rejections


def split_reusable(conn, selected: list[dict]) -> tuple[list[dict], list[dict]]:
    """把入選話題分成 (有現成文章可重用的, 要新生成的)。

    只要這個話題以前生成過文章就直接重用（含信心度、素材清單、翻譯快取），
    不看它現在的出刊標記：文章內容跟選題分數無關，重寫一次是重花一次 LLM
    又拿到一篇沒人看過的新文章。週報重用日報的文章、重建歷史期數時重用
    上一輪的文章，走的都是這條。回傳的第一個 list 元素已經是 results 的
    形狀，可以直接跟 generate_and_check() 的輸出合併。2026-08-25 加。
    """
    generated_map = get_latest_generated_for_topics(conn, [e["row"]["id"] for e in selected])
    reused: list[dict] = []
    to_generate: list[dict] = []
    for entry in selected:
        topic_id = entry["row"]["id"]
        previous = generated_map.get(topic_id)
        if previous is None:
            to_generate.append(entry)
            continue
        reused.append(
            {
                "topic_id": topic_id,
                "row": entry["row"],
                "article": json.loads(previous["generated_json"]),
                "source_article_ids": json.loads(previous["source_article_ids_json"]),
                "confidence": previous["confidence"],
                "needs_review": bool(previous["needs_review"]),
                "selected_via": {**(entry.get("selected_via") or {}), "reused_from_issue": previous["issue_id"]},
                "source_detail": None,
                "single_source": entry.get("single_source"),
                "translations_json": previous["translations_json"],
            }
        )
    return reused, to_generate


def _selected_trace_entries(results: list[dict]) -> list[dict]:
    """入選的也要記帳，而且要記「它是怎麼被選上的」。

    選題帳上最常被問的就是這一欄：這篇憑什麼上？答案是某個模組給了幾分、
    在第幾輪被挑走的，這些值在選題當下就算好了，不記下來事後算不回來
    （下一期的候選池已經不一樣了）。
    """
    return [
        {
            "topic_id": r["topic_id"],
            "decision": "selected",
            "reason": None,
            "stage": "selection",
            "detail": {
                "selected_via": r.get("selected_via"),
                "sources": r.get("source_detail"),
                "single_source": r.get("single_source"),
                "confidence": round(r["confidence"], 2),
                "needs_review": bool(r["needs_review"]),
            },
        }
        for r in results
    ]


def _print_ledger(selected_count: int, results: list[dict], rejections: list[dict]) -> None:
    """把這一期的選題帳印出來。網頁上有同一份資料的頁面，但排程是無人值守
    跑的，log 裡看得到才不用每天去開網頁。"""
    from collections import Counter

    print(f"[compose_topic_issue] 選題帳：候選 {selected_count + len(rejections)} 個")
    print(f"[compose_topic_issue]   入選並成功產出：{len(results)} 個")
    counts = Counter(r["reason"] for r in rejections)
    unqualified = {k: v for k, v in counts.items() if not gates.is_quota_reason(k)}
    quota_full = {k: v for k, v in counts.items() if gates.is_quota_reason(k)}
    if unqualified:
        print("[compose_topic_issue]   不夠格：")
        for reason, count in sorted(unqualified.items(), key=lambda kv: -kv[1]):
            print(f"[compose_topic_issue]     {count:>4} 個  {gates.label_for(reason)}")
    if quota_full:
        print("[compose_topic_issue]   夠格但沒版位：")
        for reason, count in sorted(quota_full.items(), key=lambda kv: -kv[1]):
            print(f"[compose_topic_issue]     {count:>4} 個  {gates.label_for(reason)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date", default=date.today().isoformat(),
        help="這期涵蓋的日期（ISO date）。--cadence daily 時同時當作 issue_date/period_start/period_end；"
        "--cadence weekly 時當作 issue_date，候選池窗口自動取這一天之前的上一個完整週一到週日。預設今天。",
    )
    parser.add_argument(
        "--cadence", default="daily", choices=["daily", "weekly"],
        help="出刊頻率，決定用 config/topics.yaml 的 selection.daily 還是 selection.weekly 配額。預設 daily。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    conn = get_connection(config["database"]["path"])

    # 冪等防護：同一天同一種頻率已經出過就不重複出。排程錯過補跑、手動
    # 重跑、回填重疊時都會撞到這個情況，沒有防護會出兩期一模一樣日期的刊。
    existing = conn.execute(
        "SELECT id FROM issues WHERE issue_date = ? AND cadence = ?", (args.date, args.cadence)
    ).fetchone()
    if existing:
        print(f"[compose_topic_issue] {args.date} 的 {args.cadence} 已經有第 {existing['id']} 期，不重複出。")
        return

    # 日報候選池的日期範圍。2026-08-14 從「只看當天」改成往回多看
    # carry_over_days 天：舊行為下，昨天因為版位滿而落選的話題今天不會再進
    # 候選（它底下的文章不是今天發布的），帳面寫「版位滿」、行為上是永久
    # 淘汰，實測 299 個已打分未出刊的話題（91.7%）就這樣再也沒被考慮過。
    # 續留窗口內每天重新參選，靠熱度的時間衰減自然降權；過了窗口就不再進
    # 候選，等於自然過期。
    if args.cadence == "daily":
        carry_days = config["selection"]["daily"].get("carry_over_days", 0)
        range_start = (date.fromisoformat(args.date) - timedelta(days=carry_days)).isoformat()
        date_range = (range_start, args.date)
    else:
        # 週報涵蓋「上一個完整的週日到週六」（一週從週日起算，2026-08-25
        # 使用者定的；8 月第一週因此是 7/26 到 8/1）。週一中午出刊時往回找
        # 最近一個已經完整結束的週：weekday() 週一=0…週日=6，先退到本週的
        # 週日再退 7 天。舊行為是不限日期選整池，改成固定週窗口配合候選池
        # 含已上日報話題（見 pipeline/topic_db.py 的 get_available_topics()）。
        d = date.fromisoformat(args.date)
        days_since_sunday = (d.weekday() + 1) % 7
        week_start = d - timedelta(days=days_since_sunday + 7)
        date_range = (week_start.isoformat(), (week_start + timedelta(days=6)).isoformat())
        print(f"[compose_topic_issue] 週報窗口：{date_range[0]} ~ {date_range[1]}")
    selected, rejections = select_for_issue(conn, config, cadence=args.cadence, date_range=date_range)
    print(f"[compose_topic_issue] 入選話題：{len(selected)} 個，落選 {len(rejections)} 個")
    if not selected:
        # 沒選出東西也要留帳：這一天到底是「沒有候選」還是「候選都不夠格」，
        # 是兩件完全不同的事，事後只看「今天沒出刊」分不出來。
        record_selection_trace(
            conn, issue_date=args.date, cadence=args.cadence, entries=rejections
        )
        print("[compose_topic_issue] 話題池裡沒有可用話題（或都已經被選用過），中止。")
        _print_ledger(0, [], rejections)
        return

    if args.cadence == "weekly":
        reused, to_generate = split_reusable(conn, selected)
        if reused:
            print(f"[compose_topic_issue] {len(reused)} 篇重用日報已生成的文章，{len(to_generate)} 篇要新生成")
        results, gen_rejections = generate_and_check(conn, to_generate, config)
        results = reused + results
        # 合併後還原選題時排好的版面順序（週度潛力分高的在前）。
        order = {e["row"]["id"]: i for i, e in enumerate(selected)}
        results.sort(key=lambda r: order[r["topic_id"]])
    else:
        results, gen_rejections = generate_and_check(conn, selected, config)
    rejections.extend(gen_rejections)

    if not results:
        record_selection_trace(
            conn, issue_date=args.date, cadence=args.cadence, entries=rejections
        )
        print("[compose_topic_issue] 入選話題全部生成失敗，沒有組成新的一期，中止。")
        _print_ledger(len(selected), [], rejections)
        return

    period_start, period_end = date_range if date_range else (None, None)
    issue_id = create_issue(conn, args.date, period_start=period_start, period_end=period_end, cadence=args.cadence)
    for r in results:
        save_generated_topic(
            conn, issue_id, r["topic_id"], r["article"], r["source_article_ids"],
            r["confidence"], r["needs_review"],
            translations_json=r.get("translations_json"),
        )
    mark_topics_published(conn, [r["topic_id"] for r in results], issue_id, cadence=args.cadence)

    record_selection_trace(
        conn,
        issue_date=args.date,
        cadence=args.cadence,
        entries=_selected_trace_entries(results) + rejections,
        issue_id=issue_id,
    )
    attach_trace_to_issue(conn, args.date, args.cadence, issue_id)

    ok, failed = pretranslate_issue(conn, issue_id)
    print(f"[compose_topic_issue] 英文版預先翻譯：成功 {ok} 篇，失敗 {failed} 篇。")

    pending = sum(1 for r in results if r["needs_review"])
    print(f"[compose_topic_issue] 第 {issue_id} 期已組成，{pending} 個待人工確認。")
    _print_ledger(len(selected), results, rejections)
    print("[compose_topic_issue] 用 python -m scripts.serve_topics 啟動網頁查看。")


if __name__ == "__main__":
    main()
