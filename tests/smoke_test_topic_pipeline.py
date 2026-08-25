"""離線煙霧測試（話題式週報，第三條 pipeline）：驗證入池去重、話題聚類、
18模組選題配額、reranker真實檢索、自檢重試迴圈、發布後不重選、網頁能讀出
資料，這些邏輯沒有壞掉。

跟 tests/smoke_test_case_pipeline.py 同樣的手法：輸入端一律用真實 RSS
抓取，只 mock 會花錢的 LLM 呼叫（標籤抽取、18模組打分、寫作、自檢）。
不一樣的地方：embedding（Qwen3-Embedding-0.6B）、Qdrant 聚類、reranker
（bge-reranker-v2-m3）這三塊完全不 mock，因為它們跑在本機、不花錢、不需要
LLM_API_KEY，這是這次「建架構、來測試」在沒有 LLM key 前提下能做到的
最大真實驗證範圍。

用獨立的測試用 SQLite 檔跟 Qdrant 路徑（跑之前會清掉），不會動到真正的
data/topics.db、data/qdrant。第一次執行需要下載 embedding/reranker 模型
權重（合計約 2-3GB），之後有快取就快了。

用法：
    python -m tests.smoke_test_topic_pipeline
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import patch

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from generation.topic_generate import generate_topic_article
from pipeline import gates
from pipeline.article_tagging import tag_article
from pipeline.module_scoring import score_topic
from pipeline.retrieval import retrieve_sources_for_topic
from pipeline.topic_clustering import cluster_new_articles
from pipeline.topic_db import (
    create_issue,
    get_articles_for_topic,
    get_available_topics,
    get_connection,
    get_untagged_articles,
    get_unscored_topics,
    insert_article_if_new,
    mark_topics_published,
    save_article_gate,
    save_article_tags,
    save_generated_topic,
    save_module_scores,
    week_start_date,
)
from pipeline.topic_selection import select_for_issue
from review.topic_selfcheck import self_check
from scripts.ingest_topics import _fetch_source_items
from ingestion.base import RawItem
from datetime import datetime

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
TEST_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "smoke_test_topics.db"
TEST_QDRANT_PATH = Path(__file__).resolve().parent.parent / "data" / "smoke_test_qdrant"

_MODULE_ID_CYCLE_STATE = {"i": 0}


# --- 假的 LLM client：只換掉會花錢的那幾段（標籤/打分/寫作/自檢），
#     其他都是真資料真邏輯 ---


def _fake_tagging() -> str:
    modes = ["watch", "use", "mixed"]
    mode = modes[_MODULE_ID_CYCLE_STATE["i"] % len(modes)]
    _MODULE_ID_CYCLE_STATE["i"] += 1
    return json.dumps(
        {
            "content_mode": mode,
            "is_case_example": True,
            "case_industry": "零售",
            "case_department": "供應鏈",
            "case_outcome": "成功",
            "tech_tags": ["RAG", "煙霧測試假標籤"],
            "entity_tags": ["假公司"],
            "scenario_tags": ["需求預測"],
            "industry_tags": ["零售"],
            "one_line_summary": "煙霧測試假標籤：某公司用AI改善了某個流程",
        },
        ensure_ascii=False,
    )


def _fake_module_scoring(modules_config: dict) -> str:
    import random

    content_types = ["insight", "practical", "warning", "flash"]
    module_ids = [m["id"] for g in ("functional", "domain") for m in modules_config[g]]
    # 隨機但可重現的分數分佈，確保各模組、各 content_type 都有機會被測到
    rng = random.Random(_MODULE_ID_CYCLE_STATE["i"])
    _MODULE_ID_CYCLE_STATE["i"] += 1
    scores = {mid: {"score": round(rng.uniform(0, 10), 1), "reason": "煙霧測試假理由"} for mid in module_ids}
    return json.dumps(
        {"module_scores": scores, "content_type": rng.choice(content_types)}, ensure_ascii=False
    )


def _fake_generate() -> str:
    return json.dumps(
        {
            "headline_candidates": ["假標題一", "假標題二", "假標題三"],
            "chosen_headline": "煙霧測試用假標題",
            "sections": [
                {"heading": "發生了什麼", "paragraphs": ["假段落。"]},
                {"heading": "為什麼重要", "paragraphs": ["假段落。"]},
            ],
            "stats": [{"value": "30%", "label": "時間節省"}],
            "delta_insight": {"paragraphs": ["可以想像用在IT helpdesk。"]},
            "card_summary": {"text": "假摘要文案。", "metric_value": "30%", "metric_label": "時間節省"},
        },
        ensure_ascii=False,
    )


_selfcheck_calls: dict[str, int] = {}


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


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.choices = [_FakeChoice(text)]


def _make_fake_completions(modules_config: dict):
    class FakeCompletions:
        def create(self, *, messages: list[dict], **kwargs):
            system = messages[0]["content"]
            user = messages[1]["content"]
            if "資料標註員" in system:
                return _FakeResponse(_fake_tagging())
            if "選題編輯" in system:
                return _FakeResponse(_fake_module_scoring(modules_config))
            if "審查員" in system:
                return _FakeResponse(_fake_self_check(user))
            return _FakeResponse(_fake_generate())

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.chat = FakeChat()

    return FakeClient


def _row_to_raw_item(row) -> RawItem:
    return RawItem(
        title=row["title"],
        url=row["url"],
        source="case_study",
        subdomain_id=row["source_id"],
        published_at=datetime.fromisoformat(row["published_at"]),
        summary=row["content"],
        score=row["source_weight"],
        extra={"source_name": row["source_name"], "source_weight": row["source_weight"]},
    )


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    test_config = copy.deepcopy(config)
    test_config["database"]["path"] = str(TEST_DB_PATH)
    test_config["vector_store"]["path"] = str(TEST_QDRANT_PATH)

    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    if TEST_QDRANT_PATH.exists():
        shutil.rmtree(TEST_QDRANT_PATH)
    conn = get_connection(str(TEST_DB_PATH))

    print("[smoke_test] 抓取真實候選文章（連網，不用API key）...")
    # 直接用正式流程的來源分派函式，不要在這裡自己寫一份。
    # 這支以前寫死「每個來源都是 RSS、都有 url 欄位」，config 後來加了
    # arXiv／HN／Reddit／GitHub／StackExchange 之後就一路 KeyError: 'url'，
    # 但沒人發現，因為測試沒有跑在任何自動流程裡。
    candidates = []
    for source in config["sources"]:
        try:
            candidates += _fetch_source_items(source, config["fetch"])
        except Exception as exc:  # noqa: BLE001 -- 跟正式流程一樣，單一來源失敗不中斷
            print(f"[smoke_test]   來源 {source['name']} 抓取失敗，跳過：{exc}")
    assert candidates, "沒有抓到任何真實候選文章，無法繼續煙霧測試"
    print(f"[smoke_test] 真實候選文章：{len(candidates)} 篇")

    inserted_ids = []
    gate_counts = Counter()
    for item in candidates:
        item.extra.setdefault("tier", "case")
        article_id = insert_article_if_new(conn, item)
        if article_id is None:
            continue
        inserted_ids.append(article_id)
        # Gate 1a：跟正式流程一樣，入池當下就判定
        result = gates.check_article_intake(
            content=item.summary, published_at=item.published_at, config=test_config
        )
        save_article_gate(conn, article_id, result)
        gate_counts[result.status] += 1
    print(f"[smoke_test] 入池（去重後）：{len(inserted_ids)} 篇")
    print(
        f"[smoke_test] 收錄判定：可寫作 {gate_counts['included']} 篇、"
        f"只當熱度訊號 {gate_counts['signal_only']} 篇、排除 {gate_counts['excluded']} 篇"
    )
    assert gate_counts["included"], "所有文章都沒通過收錄判定，門檻是不是設太嚴"

    reinsert_ids = [insert_article_if_new(conn, item) for item in candidates]
    assert all(aid is None for aid in reinsert_ids), "同網址文章重複入池，去重邏輯壞了"
    print("[smoke_test] 重複入池去重驗證通過")

    print("[smoke_test] 話題聚類中（真實 embedding + Qdrant，不 mock，第一次跑要下載模型權重）...")
    embed_cfg = test_config["embedding"]
    vector_cfg = test_config["vector_store"]
    cluster_stats = cluster_new_articles(
        conn,
        qdrant_path=vector_cfg["path"],
        collection=vector_cfg["collection"],
        similarity_threshold=embed_cfg["cluster_similarity_threshold"],
    )
    assert cluster_stats["processed"] == len(inserted_ids), "聚類處理的文章數量跟入池數量對不上"
    print(
        f"[smoke_test] 聚類完成：{cluster_stats['new_topics']} 個新話題、"
        f"{cluster_stats['merged']} 篇併入既有話題（目前來源都是企業案例，預期以新開話題為主）"
    )

    FakeClient = _make_fake_completions(config["modules"])

    with patch("openai.OpenAI", FakeClient):
        week_start = week_start_date()
        to_tag = get_untagged_articles(conn, week_start)
        assert len(to_tag) == len(inserted_ids), "待標籤文章數量跟入池數量對不上"
        for row in to_tag:
            item = _row_to_raw_item(row)
            parsed = tag_article(item)
            save_article_tags(
                conn,
                row["id"],
                content_mode=parsed["content_mode"],
                is_case_example=parsed["is_case_example"],
                case_industry=parsed.get("case_industry"),
                case_department=parsed.get("case_department"),
                case_outcome=parsed.get("case_outcome"),
                tech_tags=parsed["tech_tags"],
                entity_tags=parsed["entity_tags"],
                scenario_tags=parsed["scenario_tags"],
                industry_tags=parsed["industry_tags"],
                one_line_summary=parsed["one_line_summary"],
            )
        assert get_untagged_articles(conn, week_start) == [], "還有文章沒標籤完"
        print(f"[smoke_test] {len(to_tag)} 篇文章標籤完成")

        to_score = get_unscored_topics(conn)
        for topic_row in to_score:
            articles = get_articles_for_topic(conn, topic_row["id"])
            parsed = score_topic(topic_row, articles, config["modules"])
            save_module_scores(conn, topic_row["id"], parsed["module_scores"], parsed["content_type"])
        assert get_unscored_topics(conn) == [], "還有話題沒打分完"
        print(f"[smoke_test] {len(to_score)} 個話題打分完成")

        available = get_available_topics(conn)
        assert len(available) == len(to_score), "打分後可選話題數量對不上，是不是哪裡把話題弄丟了"

        selected, rejections = select_for_issue(conn, test_config)
        selection_cfg = test_config["selection"]["weekly"]
        total_min, total_max = selection_cfg["total_topics"]
        assert len(selected) <= total_max
        type_counts = Counter(e["content_type"] for e in selected)
        for ctype, (_qmin, qmax) in selection_cfg["content_type_quota"].items():
            assert type_counts[ctype] <= qmax, f"{ctype} 選超過配額上限"
        # 選題帳：入選加落選要等於候選總數，不能有話題「不見了」。這正是加
        # selection_trace 要防的事（見 pipeline/topic_selection.py）。
        assert len(selected) + len(rejections) == len(available), (
            f"候選 {len(available)} 個，帳只記到 {len(selected) + len(rejections)} 個，有話題沒被記帳"
        )
        for r in rejections:
            assert r["reason"], "落選一定要有理由碼"
            assert r["decision"] == "rejected"
        print(
            f"[smoke_test] 選題驗證通過，入選 {len(selected)} 個話題，"
            f"落選 {len(rejections)} 個都有理由碼，型態分布：{dict(type_counts)}"
        )

        newsletter_name = test_config["newsletter"]["name"]
        results = []
        for entry in selected:
            topic_row = entry["row"]
            content_type = entry["content_type"]

            source_rows, source_detail = retrieve_sources_for_topic(conn, test_config, topic_row)
            assert source_rows, "沒有回傳任何來源，寫作素材會是空的"
            assert len(source_rows) <= test_config["sources_for_writing"]["max_sources"]
            # 素材必須屬於這個話題本身。全池補充預設關閉，開著的話這條會擋下
            # 「話題講 A、素材講 B」那種組合（見 pipeline/retrieval.py 的說明）。
            if not test_config["sources_for_writing"]["supplement"]["enabled"]:
                own_ids = {row["id"] for row in get_articles_for_topic(conn, topic_row["id"])}
                stray = [r["id"] for r in source_rows if r["id"] not in own_ids]
                assert not stray, f"素材裡混進了不屬於這個話題的文章：{stray}"
            print(
                f"[smoke_test]   話題「{topic_row['representative_title'][:30]}」"
                f"取得 {len(source_rows)} 篇素材（{source_detail}）"
            )

            article = generate_topic_article(newsletter_name, topic_row["representative_title"], content_type, source_rows)
            check = self_check(article, source_rows)
            assert check["confidence"] < 0.8, "第一次自檢應該模擬信心度不足以觸發重試"

            article = generate_topic_article(
                newsletter_name, topic_row["representative_title"], content_type, source_rows,
                check["revision_instructions"],
            )
            check = self_check(article, source_rows)
            assert check["confidence"] >= 0.8, "重新生成後自檢應該要通過門檻"

            results.append(
                {
                    "topic_id": topic_row["id"],
                    "row": topic_row,
                    "article": article,
                    "source_article_ids": [row["id"] for row in source_rows],
                    "confidence": check["confidence"],
                    "needs_review": False,
                }
            )
        print(f"[smoke_test] 自檢重試迴圈驗證通過（{len(results)} 個話題都從低信心度修到通過門檻）")

        issue_id = create_issue(conn, date.today().isoformat())
        for r in results:
            save_generated_topic(
                conn, issue_id, r["topic_id"], r["article"], r["source_article_ids"],
                r["confidence"], r["needs_review"],
            )
        mark_topics_published(conn, [r["topic_id"] for r in results], issue_id)

        pool_after = get_available_topics(conn)
        published_ids = {r["topic_id"] for r in results}
        assert not any(row["id"] in published_ids for row in pool_after), (
            "已發布的話題還留在可選池裡，下一期可能會重選到同一個話題"
        )
        print("[smoke_test] 發布後標記驗證通過：已選用話題不會被下一期重選")

    # 網頁：monkeypatch config 指向這支測試用的 DB，不動到真正的 data/topics.db
    import scripts.serve_topics as serve_module
    from fastapi.testclient import TestClient

    serve_module._config["database"]["path"] = str(TEST_DB_PATH)
    client = TestClient(serve_module.app)

    resp_list = client.get("/")
    assert resp_list.status_code == 200 and f"第 {issue_id} 期" in resp_list.text
    resp_detail = client.get(f"/issues/{issue_id}")
    assert resp_detail.status_code == 200
    assert results[0]["article"]["chosen_headline"] in resp_detail.text
    print("[smoke_test] 網頁渲染驗證通過（期數列表 + 單期詳細頁都能正確讀出DB資料）")

    print("[smoke_test] 全部通過。")


if __name__ == "__main__":
    main()
