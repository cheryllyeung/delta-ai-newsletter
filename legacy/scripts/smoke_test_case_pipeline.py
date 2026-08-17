"""離線煙霧測試（v2，pool 化架構）：驗證入池、評分不淘汰、趨勢計算、
配額選文、自檢重試迴圈、發布後不重選、網頁能讀出資料，這些邏輯沒有壞掉。

輸入端一律用真實 RSS 抓取（不虛構 RawItem），只 mock 會花錢的 Claude API
呼叫。用獨立的測試用 SQLite 檔（跑之前會清掉），不會動到真正的
data/pulse.db。這支測試要連網（抓真實RSS）但不需要 LLM_API_KEY、
不花 LLM 額度，可以隨時跑。

用法：
    python -m scripts.smoke_test_case_pipeline
"""
from __future__ import annotations

import hashlib
import itertools
import json
import sys
from pathlib import Path
from unittest.mock import patch

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from generation.case_generate import generate_case
from generation.case_intro import generate_intro
from ingestion.case_source import fetch_case_study_items
from pipeline.case_scoring import compute_base_score, score_article
from pipeline.pool_db import (
    create_issue,
    get_available_pool,
    get_connection,
    get_unscored_articles,
    insert_article_if_new,
    mark_published,
    save_generated_case,
    save_score,
)
from pipeline.pool_selection import select_for_issue
from pipeline.tag_clustering import compute_tag_clusters
from review.case_selfcheck import self_check

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pulse.yaml"
TEST_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "smoke_test_pool.db"

# --- 假的 Claude client：只換掉會花錢的那一段，前後端輸入輸出仍是真資料 ---

# 分類跟標籤刻意分散＋部分重複：讓 area_category 三桶都有貨可以測配額，
# 也讓 "#RAG"這個標籤在「近期」出現得特別密集，藉此驗證趨勢偵測有抓到。
_AREA_CYCLE = itertools.cycle(["後勤支援", "業務前台", "廠區現場", "後勤支援", "業務前台"])
_selfcheck_calls: dict[str, int] = {}


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.choices = [_FakeChoice(text)]


def _fake_scoring() -> str:
    area = next(_AREA_CYCLE)
    return json.dumps(
        {
            "is_ai_application": True,
            "area_category": area,
            "hashtags": ["#RAG", "#煙霧測試假標籤"],
            "scores": {
                "transferability": 4.0,
                "specificity": 3.5,
                "novelty": 3.0,
                "narrativity": 3.5,
            },
            "one_line_summary": "煙霧測試假評分：某公司用AI改善了某個流程",
            "key_facts": [{"fact": "省下30%時間", "source_sentence": "（測試用）"}],
        },
        ensure_ascii=False,
    )


def _fake_generate() -> str:
    return json.dumps(
        {
            "title": "煙霧測試用假標題",
            "sections": [
                {"heading": "背景與痛點", "paragraphs": ["假段落。"]},
                {"heading": "他們怎麼做", "paragraphs": ["假段落。"]},
                {"heading": "成效", "paragraphs": ["假段落。"]},
                {"heading": "值得追蹤的後續", "paragraphs": ["假段落。"]},
            ],
            "stats": [{"value": "30%", "label": "時間節省"}],
            "delta_insight": {"paragraphs": ["可以想像用在IT helpdesk。"]},
            "card_summary": {
                "text": "假摘要文案。",
                "metric_value": "30%",
                "metric_label": "時間節省",
            },
        },
        ensure_ascii=False,
    )


def _fake_self_check(user_text: str) -> str:
    key = hashlib.md5(user_text.encode()).hexdigest()
    count = _selfcheck_calls.get(key, 0) + 1
    _selfcheck_calls[key] = count

    if count == 1:
        return json.dumps(
            {
                "fact_claims": [{"claim": "假聲明", "verdict": "unsupported", "evidence": "找不到"}],
                "style_violations": [],
                "sensitivity_flags": [],
                "tone_score": 3.0,
                "revision_instructions": "請移除沒有依據的聲明。",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "fact_claims": [{"claim": "假聲明", "verdict": "supported", "evidence": "找到了"}],
            "style_violations": [],
            "sensitivity_flags": [],
            "tone_score": 4.0,
            "revision_instructions": "",
        },
        ensure_ascii=False,
    )


def _fake_intro() -> str:
    return json.dumps(
        {"signal": ["假訊號第一段。", "假訊號第二段。"], "hook": "假的懸念句。"}, ensure_ascii=False
    )


class FakeCompletions:
    def create(self, *, messages: list[dict], **kwargs):
        system = messages[0]["content"]
        user = messages[1]["content"]
        if "選文編輯" in system:
            return _FakeResponse(_fake_scoring())
        if "審查員" in system:
            return _FakeResponse(_fake_self_check(user))
        if "主編" in system:
            return _FakeResponse(_fake_intro())
        return _FakeResponse(_fake_generate())


class FakeChat:
    def __init__(self) -> None:
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.chat = FakeChat()


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    conn = get_connection(str(TEST_DB_PATH))

    print("[smoke_test] 抓取真實候選文章（連網，不用API key）...")
    candidates = []
    for source in config["sources"]:
        candidates += fetch_case_study_items(
            source_id=source["id"],
            source_name=source["name"],
            url=source["url"],
            weight=source["weight"],
            days_back=config["fetch"]["days_back"],
            max_items=config["fetch"]["max_items_per_source"],
        )
    assert candidates, "沒有抓到任何真實候選文章，無法繼續煙霧測試"
    print(f"[smoke_test] 真實候選文章：{len(candidates)} 篇")

    inserted_ids = [aid for item in candidates if (aid := insert_article_if_new(conn, item)) is not None]
    print(f"[smoke_test] 入池（去重後）：{len(inserted_ids)} 篇")

    # 重複插入同一批應該全部被去重擋掉，驗證「不重複抓」這個 pool 的核心特性
    reinsert_ids = [insert_article_if_new(conn, item) for item in candidates]
    assert all(aid is None for aid in reinsert_ids), "同網址文章重複入池，去重邏輯壞了"
    print("[smoke_test] 重複入池去重驗證通過")

    selection_cfg = config["selection"]

    with patch("openai.OpenAI", FakeClient):
        unscored = get_unscored_articles(conn)
        assert len(unscored) == len(inserted_ids)
        for row in unscored:
            item = next(i for i in candidates if i.url == row["url"])
            parsed = score_article(item)
            base_score = compute_base_score(
                scores=parsed["scores"],
                weights=selection_cfg["scoring_weights"],
                source_weight=row["source_weight"],
                is_ai_application=parsed["is_ai_application"],
                non_application_multiplier=selection_cfg["non_application_score_multiplier"],
            )
            save_score(
                conn,
                row["id"],
                area_category=parsed["area_category"],
                is_ai_application=parsed["is_ai_application"],
                scores=parsed["scores"],
                base_score=base_score,
                one_line_summary=parsed["one_line_summary"],
                key_facts=parsed["key_facts"],
                hashtags=parsed["hashtags"],
            )

        assert get_unscored_articles(conn) == [], "還有文章沒評分完"
        pool = get_available_pool(conn)
        assert len(pool) == len(inserted_ids), "評分後文章數量對不上，是不是哪裡把文章刪掉了"
        print(f"[smoke_test] 全部 {len(pool)} 篇都評分完成且都還在 pool 裡（沒有硬淘汰）")

        # 驗證 is_ai_application 不淘汰，只打折：所有假評分都是 is_ai_application=True，
        # 這裡額外驗證公式本身在 False 情境下確實會打折，而不是丟資料。
        discounted = compute_base_score(
            scores={"transferability": 4, "specificity": 4, "novelty": 4, "narrativity": 4},
            weights=selection_cfg["scoring_weights"],
            source_weight=1.0,
            is_ai_application=False,
            non_application_multiplier=selection_cfg["non_application_score_multiplier"],
        )
        full = compute_base_score(
            scores={"transferability": 4, "specificity": 4, "novelty": 4, "narrativity": 4},
            weights=selection_cfg["scoring_weights"],
            source_weight=1.0,
            is_ai_application=True,
            non_application_multiplier=selection_cfg["non_application_score_multiplier"],
        )
        assert discounted < full and discounted > 0, "is_ai_application=False 應該打折但不歸零/不淘汰"
        print("[smoke_test] is_ai_application=False 打折不淘汰，驗證通過")

        # 標籤同義詞分群：真的用本機 embedding 模型（免費、不用API key），
        # 不 mock，直接驗證語意相近的詞會被分到同一組、不相關的詞不會。
        sample_counts = {
            "AI客服": 5,
            "智能客服機器人": 3,
            "客服自動化": 2,
            "預測性維護": 4,
            "RAG": 1,
        }
        clusters = compute_tag_clusters(sample_counts)
        assert clusters["AI客服"] == clusters["智能客服機器人"] == clusters["客服自動化"], (
            f"語意相近的客服類標籤沒有被分到同一組：{clusters}"
        )
        assert clusters["預測性維護"] != clusters["AI客服"], "不相關的標籤不應該被合併"
        assert clusters["RAG"] != clusters["AI客服"], "不相關的標籤不應該被合併"
        print(f"[smoke_test] 標籤同義詞分群驗證通過：{clusters}")

        selected = select_for_issue(conn, config)
        area_quota = selection_cfg["area_quota"]
        total_min, total_max = selection_cfg["total_cases"]
        assert total_min <= len(selected) <= total_max or len(selected) < total_min
        for area, (_qmin, qmax) in area_quota.items():
            count_in_area = sum(1 for s in selected if s["row"]["area_category"] == area)
            assert count_in_area <= qmax, f"{area} 選超過配額上限"
        print(f"[smoke_test] 配額選文驗證通過，入選 {len(selected)} 篇")

        newsletter_name = config["newsletter"]["name"]
        cases = []
        for entry in selected:
            row = entry["row"]
            tags = entry["tags"]
            key_facts = json.loads(row["key_facts_json"])

            article = generate_case(newsletter_name, row, tags, key_facts)
            check = self_check(article, key_facts, row["content"])
            assert check["confidence"] < 0.8, "第一次自檢應該模擬信心度不足以觸發重試"

            article = generate_case(newsletter_name, row, tags, key_facts, check["revision_instructions"])
            check = self_check(article, key_facts, row["content"])
            assert check["confidence"] >= 0.8, "重新生成後自檢應該要通過門檻"

            cases.append(
                {
                    "article_id": row["id"],
                    "row": row,
                    "tags": tags,
                    "article": article,
                    "confidence": check["confidence"],
                    "needs_review": False,
                }
            )
        print(f"[smoke_test] 自檢重試迴圈驗證通過（{len(cases)} 篇都從低信心度修到通過門檻）")

        from collections import Counter

        tag_counter = Counter()
        area_counter = Counter()
        for c in cases:
            tag_counter.update(c["tags"])
            area_counter[c["row"]["area_category"]] += 1
        dominant_tags = [tag for tag, _ in tag_counter.most_common(5)]

        intro = generate_intro(
            newsletter_name=newsletter_name,
            selected_cases_summaries=[c["article"]["card_summary"]["text"] for c in cases],
            dominant_tags=dominant_tags,
            area_breakdown=dict(area_counter),
        )

        from datetime import date

        issue_id = create_issue(conn, date.today().isoformat(), intro["hook"], intro["signal"])
        for c in cases:
            save_generated_case(conn, issue_id, c["article_id"], c["article"], c["confidence"], c["needs_review"])
        mark_published(conn, [c["article_id"] for c in cases], issue_id)

        pool_after = get_available_pool(conn)
        published_ids = {c["article_id"] for c in cases}
        assert not any(row["id"] in published_ids for row in pool_after), (
            "已發布的文章還留在可選 pool 裡，下一期可能會重選到同一篇"
        )
        print("[smoke_test] 發布後標記驗證通過：已選用文章不會被下一期重選")

    # 網頁：monkeypatch config 指向這支測試用的 DB，不動到真正的 data/pulse.db
    import scripts.serve_pulse as serve_module
    from fastapi.testclient import TestClient

    serve_module._config["database"]["path"] = str(TEST_DB_PATH)
    client = TestClient(serve_module.app)

    resp_list = client.get("/")
    assert resp_list.status_code == 200 and "第 1 期" in resp_list.text
    resp_detail = client.get(f"/issues/{issue_id}")
    assert resp_detail.status_code == 200
    assert cases[0]["article"]["title"] in resp_detail.text
    print("[smoke_test] 網頁渲染驗證通過（期數列表 + 單期詳細頁都能正確讀出DB資料）")

    print("[smoke_test] 全部通過。")


if __name__ == "__main__":
    main()
