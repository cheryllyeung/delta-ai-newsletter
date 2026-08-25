"""階段三：對已打分的話題池做組合最佳化選題（依 docs/prd/0731_PRD_v0.5.md 階段三）。

跟打分（pipeline/module_scoring.py）完全脫鉤：這裡只處理「已經打過18模組
分數、還沒被任何一期選用過」的話題。

選題邏輯分三輪：
1. 模組輪動：18 個模組各自找目前分數最高、還沒入選的話題優先入選，
   確保每個模組群都有機會被照顧到，不會被少數幾個熱門話題把版位吃光
2. 補位：版位還沒滿就用跨模組總分（18個模組分數加總）排序遞補
3. 保底：如果連 total_topics 的下限都不到，才放寬 content_type 配額上限
   硬選，避免開天窗；但不會為了湊數硬選 min_module_score_to_select 以下的
   話題（那代表這期真的沒有適合的內容，比湊數更誠實）

content_type 配額比照 legacy/pipeline/pool_selection.py 的 area_quota 寫法：
某類別缺貨就從別類遞補，不會因為某類別掛零就報錯。

## 為什麼要吐落選理由（2026-08-14 加）

在這之前這支只回傳入選清單，落選的話題連同原因一起消失在函式裡。結果是
「為什麼這篇沒上」唯一能給的答案是「版位滿了」，但實際上有兩種完全不同的
情況混在一起：

| 情況 | 意思 | 該怎麼處理 |
|---|---|---|
| 不夠格 | 分數低於門檻、沒有可寫的內容 | 回頭看來源品質跟門檻設定 |
| 版位滿 | 夠格但排在後面、或某個配額已經用完 | 調 total_topics 或配額 |

現在每個候選話題的去向都會回傳給呼叫端寫進 selection_trace 表，網頁上的
選題帳直接讀它。

落選理由的歸因方式：模組輪動那一輪會多次跳過同一個話題（每個模組各判一次），
記錄「第幾次被跳過」沒有意義，所以理由是在三輪都跑完之後、拿最終的配額狀態
回頭判定的。這是近似值不是逐次重播，但對「這一期為什麼沒選它」這個問題來說
夠用，而且結果是確定性的，同一批輸入永遠得到同一份帳。
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from pipeline import gates
from pipeline.topic_db import get_articles_for_topic, get_available_topics


def compute_hotness(article_rows: list[sqlite3.Row], as_of: datetime, half_life_days: float) -> float:
    """熱門度 = 報導家數（不重複來源數）× 平均來源權重 × 時間衰減。
    時間衰減用半衰期：距離話題最後一次有新文章加入的天數每過一個半衰期，
    熱度打對折。

    這個值原本算完就沒被任何地方用到（2026-08-13 發現），現在當補位輪與保底
    輪的次要排序鍵——主鍵仍是 cross_module_total，只在總分相同時才看熱度。

    刻意不給它更大的權重：實測 191 個話題裡有 184 個底下只有一篇文章，
    「報導家數」幾乎恆等於 1，此時熱度退化成「來源權重 × 時間衰減」，鑑別力
    很低。等聚類真的把同事件的多家報導併起來（來源變多之後才會常發生），
    這個訊號才會開始有意義，屆時可以再考慮提高它的份量。

    只有標題的 signal_only 文章也算進報導家數：那正是這個狀態存在的意義，
    Hacker News 上有人貼了同一件事，即使只有一行標題，也確實構成「另一家
    在講」的證據（見 pipeline/gates.py）。
    """
    if not article_rows:
        return 0.0
    report_count = len({row["source_id"] for row in article_rows})
    avg_weight = sum(row["source_weight"] for row in article_rows) / len(article_rows)
    last_seen = max(datetime.fromisoformat(row["published_at"]) for row in article_rows)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    days_since = max((as_of - last_seen).total_seconds() / 86400, 0.0)
    time_decay = 0.5 ** (days_since / half_life_days)
    return report_count * avg_weight * time_decay


def cross_module_total(module_scores: dict) -> float:
    return sum(entry["score"] for entry in module_scores.values())


def _module_group_map(modules_config: dict) -> dict[str, str]:
    return {m["id"]: group for group in ("functional", "domain") for m in modules_config[group]}


def _dominant_group(module_scores: dict, group_map: dict[str, str]) -> str:
    """一個話題沒有單一所屬模組（18 個模組各打一次分），補位輪次要判斷
    「這個話題比較像本業還是像通用職能」時，用分數最高的那個模組代表。
    """
    top_module_id = max(module_scores, key=lambda mid: module_scores[mid]["score"])
    return group_map[top_module_id]


def _dominant_tier(articles: list[sqlite3.Row]) -> str:
    """一個話題可能聚類到多篇文章，取出現次數最多的來源 tier 代表這個話題
    （case/core/vertical/depth/signal，見 config/topics.yaml 的來源分級），
    供 tier_cap 配額判斷用。"""
    tiers = [a["tier"] or "case" for a in articles]
    return Counter(tiers).most_common(1)[0][0]


def _top_module(module_scores: dict) -> tuple[str, float]:
    module_id = max(module_scores, key=lambda mid: module_scores[mid]["score"])
    return module_id, module_scores[module_id]["score"]


def _build_scored_candidates(
    conn: sqlite3.Connection,
    config: dict,
    date_range: tuple[str, str] | None,
    cadence: str = "daily",
) -> tuple[list[dict], list[dict]]:
    """回傳 (通過 Gate 2 的候選, 被 Gate 2 擋掉的紀錄)。

    Gate 2 擋的主要是「話題底下沒有任何一篇內文夠長的文章」。這種話題在這條
    關卡加進來以前照樣會進選題、照樣可能上刊，寫作時模型拿到的只有一行標題，
    只能瞎掰。
    """
    now = datetime.now(timezone.utc)
    half_life = config["hotness"]["half_life_days"]
    group_map = _module_group_map(config["modules"])

    scored: list[dict] = []
    gate_rejected: list[dict] = []
    for row in get_available_topics(conn, date_range=date_range, cadence=cadence):
        articles = get_articles_for_topic(conn, row["id"])
        module_scores = json.loads(row["module_scores_json"]) if row["module_scores_json"] else None

        gate = gates.check_topic_ready(
            article_rows=articles, module_scores=module_scores, config=config
        )
        if not gate.passed:
            gate_rejected.append(
                {
                    "topic_id": row["id"],
                    "decision": "rejected",
                    "reason": gate.reason,
                    "stage": "selection",
                    "detail": gate.detail,
                }
            )
            continue

        scored.append(
            {
                "row": row,
                "articles": articles,
                "hotness": compute_hotness(articles, now, half_life),
                "module_scores": module_scores,
                "content_type": row["content_type"],
                "cross_module_total": cross_module_total(module_scores),
                "dominant_group": _dominant_group(module_scores, group_map),
                "dominant_tier": _dominant_tier(articles),
                "single_source": gates.is_single_source(articles),
            }
        )
    return scored, gate_rejected


def _attribute_rejection(
    entry: dict,
    *,
    min_module_score: float,
    type_counts: Counter,
    group_counts: Counter,
    tier_counts: Counter,
    quota_max,
    group_quota_max,
    tier_quota_max,
) -> tuple[str, dict]:
    """一個沒入選的候選，回傳 (理由碼, 佐證數值)。

    判定順序是有意義的：先問「它夠格嗎」再問「有位子嗎」。一個分數不夠的
    話題就算版位空著也不該入選，把它記成「版位滿」會讓帳看起來像是版面不夠
    用，實際上是內容不夠格，兩者的處理方式完全相反。
    """
    top_module_id, top_score = _top_module(entry["module_scores"])
    if top_score < min_module_score:
        return "below_module_threshold", {
            "top_module": top_module_id,
            "top_score": round(top_score, 2),
            "threshold": min_module_score,
        }

    content_type = entry["content_type"]
    if type_counts[content_type] >= quota_max(content_type):
        return "content_type_quota_full", {
            "content_type": content_type,
            "used": type_counts[content_type],
            "cap": quota_max(content_type),
        }

    group = entry["dominant_group"]
    if group_counts[group] >= group_quota_max(group):
        return "module_group_quota_full", {
            "module_group": group,
            "used": group_counts[group],
            "cap": group_quota_max(group),
        }

    tier = entry["dominant_tier"]
    if tier_counts[tier] >= tier_quota_max(tier):
        return "tier_quota_full", {
            "tier": tier,
            "used": tier_counts[tier],
            "cap": tier_quota_max(tier),
        }

    return "slots_full", {
        "top_module": top_module_id,
        "top_score": round(top_score, 2),
        "cross_module_total": round(entry["cross_module_total"], 2),
    }


def _entity_heat_counts(
    conn: sqlite3.Connection, scored: list[dict], date_range: tuple[str, str] | None
) -> dict[int, int]:
    """每個候選話題的實體熱度：窗口內有幾篇「別的」文章提到這個話題的實體。

    同一件事被聚類漏併時（實測 title-only 內容門檻切不開，見 DECISIONS.md），
    「Nvidia 這週被 14 篇文章提到」是熱度僅存的可靠訊號——它不依賴聚類有沒有
    把那 14 篇併成一個話題。用 pipeline/article_tagging.py 標好的
    entity_tags_json 精確比對；跨語言同義實體要靠知識圖譜的實體解析才併得
    起來，屆時這裡改查 graph（2026-08-25 解凍），現在先用字面比對。
    """
    query = "SELECT id, topic_id, entity_tags_json FROM articles WHERE entity_tags_json IS NOT NULL"
    params: tuple = ()
    if date_range is not None:
        query += " AND date(published_at) BETWEEN ? AND ?"
        params = date_range
    entity_to_articles: dict[str, set[int]] = {}
    article_topic: dict[int, int | None] = {}
    for row in conn.execute(query, params):
        article_topic[row["id"]] = row["topic_id"]
        for entity in json.loads(row["entity_tags_json"]):
            entity_to_articles.setdefault(entity.strip().lower(), set()).add(row["id"])

    heat: dict[int, int] = {}
    for entry in scored:
        topic_id = entry["row"]["id"]
        own_entities = {
            entity.strip().lower()
            for article in entry["articles"]
            if article["entity_tags_json"]
            for entity in json.loads(article["entity_tags_json"])
        }
        mentioning = set()
        for entity in own_entities:
            mentioning |= entity_to_articles.get(entity, set())
        heat[topic_id] = sum(1 for aid in mentioning if article_topic.get(aid) != topic_id)
    return heat


def _qualified_days_counts(
    conn: sqlite3.Connection, scored: list[dict], date_range: tuple[str, str] | None
) -> dict[int, int]:
    """每個候選話題在窗口內「夠格」的天數：入選過、或落選理由是配額類
    （夠格但沒版位）的不同日期數。

    連續多天夠格卻一直擠不上日報的話題，正是週報該撿回來的遺珠；只夠格
    一天的可能只是當天沒對手。資料直接來自 selection_trace，日報每天記帳
    的副產品，不用多花任何 LLM。
    """
    if not scored:
        return {}
    topic_ids = [entry["row"]["id"] for entry in scored]
    placeholders = ",".join("?" * len(topic_ids))
    query = f"""SELECT topic_id, decision, reason, issue_date FROM selection_trace
                WHERE cadence = 'daily' AND topic_id IN ({placeholders})"""
    params: list = list(topic_ids)
    if date_range is not None:
        query += " AND issue_date BETWEEN ? AND ?"
        params += list(date_range)
    days: dict[int, set[str]] = {}
    for row in conn.execute(query, params):
        if row["decision"] == "selected" or gates.is_quota_reason(row["reason"]):
            days.setdefault(row["topic_id"], set()).add(row["issue_date"])
    return {tid: len(dates) for tid, dates in days.items()}


def _rank_normalized(values: dict[int, float]) -> dict[int, float]:
    """把各話題的原始值換成 [0, 1] 的名次分位（值相同就同分位）。三個訊號的
    量綱完全不同（模組總分幾十、實體熱度幾篇、夠格天數個位數），直接加權
    會被量綱大的吃掉，先各自換成名次再合成。"""
    if not values:
        return {}
    distinct = sorted(set(values.values()))
    if len(distinct) == 1:
        return {tid: 0.0 for tid in values}
    position = {value: i / (len(distinct) - 1) for i, value in enumerate(distinct)}
    return {tid: position[value] for tid, value in values.items()}


def annotate_weekly_potential(
    conn: sqlite3.Connection, scored: list[dict], config: dict, date_range: tuple[str, str] | None
) -> None:
    """給每個候選補上 weekly_potential（週度潛力分）跟三個組成訊號的原始值。

    潛力分 = 名次分位合成：模組總分（選題品質）+ 實體熱度（這週有多少別的
    文章在講同一批實體）+ 夠格天數（整週持續夠格卻沒版位的遺珠訊號）。
    權重在 config 的 selection.weekly.potential_weights，2026-08-25 拍的
    起始值，沒有評測依據（見 DECISIONS.md）。
    """
    weights = config["selection"]["weekly"].get(
        "potential_weights", {"module_score": 0.5, "entity_heat": 0.25, "qualified_days": 0.25}
    )
    heat = _entity_heat_counts(conn, scored, date_range)
    qualified = _qualified_days_counts(conn, scored, date_range)

    score_rank = _rank_normalized({e["row"]["id"]: e["cross_module_total"] for e in scored})
    heat_rank = _rank_normalized({e["row"]["id"]: float(heat.get(e["row"]["id"], 0)) for e in scored})
    qualified_rank = _rank_normalized(
        {e["row"]["id"]: float(qualified.get(e["row"]["id"], 0)) for e in scored}
    )
    for entry in scored:
        topic_id = entry["row"]["id"]
        entry["entity_heat"] = heat.get(topic_id, 0)
        entry["qualified_days"] = qualified.get(topic_id, 0)
        entry["weekly_potential"] = (
            weights["module_score"] * score_rank.get(topic_id, 0.0)
            + weights["entity_heat"] * heat_rank.get(topic_id, 0.0)
            + weights["qualified_days"] * qualified_rank.get(topic_id, 0.0)
        )


def select_for_issue(
    conn: sqlite3.Connection,
    config: dict,
    cadence: str = "weekly",
    date_range: tuple[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """回傳 (入選話題清單, 落選紀錄)。

    入選的每個元素是 {"row", "articles", "hotness", "module_scores",
    "content_type", "cross_module_total", "single_source", "selected_via"}，
    其中 selected_via 記「它是怎麼被選上的」（第幾輪、被哪個模組用幾分挑上），
    這是選題帳上最常被問的一欄。

    落選的每個元素是 {"topic_id", "decision", "reason", "stage", "detail"}，
    直接餵給 pipeline.topic_db.record_selection_trace()。

    模組分兩群：domain（能源電力／樓宇自動化／電動車／網通／製造廠務／
    消費性產品／軟體平台／永續節能，台達本業）跟 functional（法務、財會、
    人資⋯泛用職能，任何公司都適用）。domain 只有 8 個、functional 有 10
    個，若不分群直接照模組清單順序輪動，functional 會先把版位挑光，
    整期內容變得像通用生產力工具介紹，跟本業無關。這裡用
    selection.<cadence>.module_group_quota 讓 domain 保底過半版位。

    cadence 選 config["selection"] 底下哪一組配額（"weekly" 或 "daily"，
    見 config/topics.yaml）。date_range 有給時只從「底下至少一篇文章
    published_at 落在這個範圍」的候選池選題（見 pipeline/topic_db.py 的
    get_available_topics() 說明）。

    cadence="weekly" 時（2026-08-25 改）：候選池含已上過日報的話題（週報
    是整週最好的回顧，不是剩菜彙整），排序鍵從單純的模組總分換成
    weekly_potential（見 annotate_weekly_potential()），date_range 傳整週
    的範圍。
    """
    scored, rejections = _build_scored_candidates(conn, config, date_range, cadence=cadence)
    if cadence == "weekly":
        annotate_weekly_potential(conn, scored, config, date_range)

    def rank_key(entry: dict):
        """補位輪與保底輪的排序鍵。日報照舊（總分、同分看熱度）；週報改用
        週度潛力分，把實體熱度跟落選帳訊號排進來。"""
        if cadence == "weekly":
            return entry["weekly_potential"]
        return (entry["cross_module_total"], entry["hotness"])

    selection_cfg = config["selection"][cadence]
    total_min, total_max = selection_cfg["total_topics"]
    content_type_quota: dict[str, list[int]] = selection_cfg["content_type_quota"]
    module_group_quota: dict[str, list[int]] = selection_cfg["module_group_quota"]
    per_module_cap = selection_cfg["per_module_cap"]
    min_module_score = selection_cfg["min_module_score_to_select"]
    tier_cap: dict[str, int] = selection_cfg.get("tier_cap", {})
    group_module_ids = {group: [m["id"] for m in config["modules"][group]] for group in ("domain", "functional")}

    selected: list[dict] = []
    selected_ids: set[int] = set()
    type_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()
    tier_counts: Counter[str] = Counter()

    def quota_max(content_type: str) -> int:
        return content_type_quota.get(content_type, [0, total_max])[1]

    def group_quota_max(group: str) -> int:
        return module_group_quota.get(group, [0, total_max])[1]

    def tier_quota_max(tier: str) -> int:
        return tier_cap.get(tier, total_max)

    def add(entry: dict, selected_via: dict) -> None:
        if cadence == "weekly":
            # 週報的選題帳要答得出「這篇憑什麼代表這一週」，把潛力分跟
            # 兩個熱度訊號的原始值一起記進去。
            selected_via = {
                **selected_via,
                "weekly_potential": round(entry["weekly_potential"], 3),
                "entity_heat": entry["entity_heat"],
                "qualified_days": entry["qualified_days"],
            }
        entry["selected_via"] = selected_via
        selected.append(entry)
        selected_ids.add(entry["row"]["id"])
        type_counts[entry["content_type"]] += 1
        group_counts[entry["dominant_group"]] += 1
        tier_counts[entry["dominant_tier"]] += 1

    # 第一輪：模組輪動，domain 群先挑（保底過半），functional 群後補；
    # 各模組取目前最高分、還沒入選、分數夠格的話題
    for group in ("domain", "functional"):
        for module_id in group_module_ids[group]:
            if len(selected) >= total_max or group_counts[group] >= group_quota_max(group):
                break
            # 週報用潛力分當同分裁決：打分鑑別力不足（最高分中位數 9.5，
            # 見 DECISIONS.md），同分很常見，這時讓整週更熱、被卡更多次的
            # 話題先上。日報維持原樣。
            candidates = sorted(
                (e for e in scored if e["row"]["id"] not in selected_ids),
                key=lambda e: (
                    (e["module_scores"][module_id]["score"], e["weekly_potential"])
                    if cadence == "weekly"
                    else e["module_scores"][module_id]["score"]
                ),
                reverse=True,
            )
            picked_for_module = 0
            for entry in candidates:
                if picked_for_module >= per_module_cap or len(selected) >= total_max:
                    break
                if group_counts[group] >= group_quota_max(group):
                    break
                if entry["module_scores"][module_id]["score"] < min_module_score:
                    break  # 排序過的候選清單，這個分數以下的更不用看
                if type_counts[entry["content_type"]] >= quota_max(entry["content_type"]):
                    continue
                if tier_counts[entry["dominant_tier"]] >= tier_quota_max(entry["dominant_tier"]):
                    continue
                add(
                    entry,
                    {
                        "round": "module_rotation",
                        "module_id": module_id,
                        "module_group": group,
                        "score": round(entry["module_scores"][module_id]["score"], 2),
                    },
                )
                picked_for_module += 1

    # 第二輪：跨模組總分排序補滿版位，一樣尊重 content_type／module_group／tier 配額上限
    if len(selected) < total_max:
        remaining = sorted(
            (e for e in scored if e["row"]["id"] not in selected_ids),
            key=rank_key,
            reverse=True,
        )
        for entry in remaining:
            if len(selected) >= total_max:
                break
            if type_counts[entry["content_type"]] >= quota_max(entry["content_type"]):
                continue
            if group_counts[entry["dominant_group"]] >= group_quota_max(entry["dominant_group"]):
                continue
            if tier_counts[entry["dominant_tier"]] >= tier_quota_max(entry["dominant_tier"]):
                continue
            top_module_id, top_score = _top_module(entry["module_scores"])
            # 補位輪原本沒有把最低分門檻判進去，等於第一輪擋掉的話題轉個彎
            # 就進來了，min_module_score_to_select 只在第一輪有效。2026-08-14
            # 補上，讓「不夠格」在三輪裡是一致的意思。
            if top_score < min_module_score:
                continue
            add(
                entry,
                {
                    "round": "cross_module_fill",
                    "module_id": top_module_id,
                    "score": round(top_score, 2),
                    "cross_module_total": round(entry["cross_module_total"], 2),
                },
            )

    # 第三輪（保底）：離下限還有距離就放寬配額上限，但不放寬最低分門檻
    if len(selected) < total_min:
        remaining = sorted(
            (e for e in scored if e["row"]["id"] not in selected_ids),
            key=rank_key,
            reverse=True,
        )
        for entry in remaining:
            if len(selected) >= total_min:
                break
            top_module_id, top_score = _top_module(entry["module_scores"])
            if top_score < min_module_score:
                continue
            add(
                entry,
                {
                    "round": "floor_fill",
                    "module_id": top_module_id,
                    "score": round(top_score, 2),
                    "note": "放寬配額上限以達到 total_topics 下限",
                },
            )

    # 版面順序：日報照總分，週報照潛力分（最有潛力最熱門的排最前面）。
    selected.sort(
        key=lambda e: e["weekly_potential"] if cadence == "weekly" else e["cross_module_total"],
        reverse=True,
    )
    if len(selected) > total_max:
        # 排序後被切掉的也是落選，要記帳，不能默默消失
        for entry in selected[total_max:]:
            selected_ids.discard(entry["row"]["id"])
        selected = selected[:total_max]

    # 沒入選的全部歸因記帳
    for entry in scored:
        if entry["row"]["id"] in selected_ids:
            continue
        reason, detail = _attribute_rejection(
            entry,
            min_module_score=min_module_score,
            type_counts=type_counts,
            group_counts=group_counts,
            tier_counts=tier_counts,
            quota_max=quota_max,
            group_quota_max=group_quota_max,
            tier_quota_max=tier_quota_max,
        )
        rejections.append(
            {
                "topic_id": entry["row"]["id"],
                "decision": "rejected",
                "reason": reason,
                "stage": "selection",
                "detail": detail,
            }
        )

    return selected, rejections
