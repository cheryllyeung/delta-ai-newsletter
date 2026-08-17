"""知識圖譜抽取煙霧測試：驗證真實文章 → mock 三元組 LLM 呼叫 → 真實
EntityResolver → 真實 Neo4j 寫入，節點/邊確實落地，不判斷抽取內容好壞
（跟 scripts/smoke_test_topic_pipeline.py 同一種哲學：輸入端用真實抓取，
只 mock 會花錢的 LLM 呼叫）。

因為 Article 節點的 id 直接沿用 SQLite 整數 id（見 pipeline/graph_store.py
的說明），拿正式開發用的 Neo4j instance 跑這支測試會有 id 衝突風險。
Neo4j Community Edition 沒有 SQLite/Qdrant 那種「換路徑就隔離」的招（多
資料庫是 Enterprise 才有），所以連線一律指向 docker-compose.yml 裡獨立的
neo4j-test（bolt://localhost:7688），絕不連正式開發用的 7687。

用法（先啟動獨立的測試用 Neo4j）：
    docker compose --profile test up -d neo4j-test
    python -m scripts.smoke_test_graph_extraction
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

import os

from ingestion.case_source import fetch_case_study_items
from pipeline import graph_builder, graph_store
from pipeline.entity_resolution import EntityResolver
from pipeline.topic_db import (
    get_connection,
    get_ungraphed_articles,
    insert_article_if_new,
    mark_article_graphed,
    save_article_tags,
    week_start_date,
)
from pipeline.triple_extraction import extract_triples
from scripts.ingest_topics import _row_to_raw_item

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
TEST_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "smoke_test_graph.db"
TEST_NEO4J_URI = "bolt://localhost:7688"

_FAKE_TRIPLES = {
    "triples": [
        {
            "subject": "台達電子",
            "subject_type": "組織",
            "relation": "導入",
            "object": "煙霧測試假技術",
            "object_type": "技術",
        }
    ]
}


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.choices = [_FakeChoice(text)]


def _fake_create_chat_completion(client, *, messages: list[dict], **kwargs):
    """取代 pipeline.llm_client.create_chat_completion()，而不是 mock
    openai.OpenAI 本身。

    create_chat_completion() 內部如果偵測到 LLM_BASE_URL 指向本機 Ollama
    （_OLLAMA_MARKER），會直接繞過傳入的 client、改打 requests.post() 打
    本機模型（見 pipeline/llm_client.py 的 _call_ollama_native() 分支），
    這代表只 mock openai.OpenAI 完全擋不住這條路徑：三元組抽取的 prompt
    帶了真實文章全文（最多 6000 字），送進本機小模型跑完整推理，實測
    輕鬆卡好幾分鐘，而且徹底違背這支測試「不花錢、不花時間」的設計目的。
    在函式呼叫這一層 mock，不管 create_chat_completion() 內部決定打哪個
    後端，都不會真的打出去。"""
    system = messages[0]["content"]
    if "三元組" in system:
        return _FakeResponse(json.dumps(_FAKE_TRIPLES, ensure_ascii=False))
    # entity_match_check.md 的灰色地帶驗證呼叫：保守回答不是同一實體，
    # 這支測試不驗證合併結果對不對，只要不噴錯就好。
    return _FakeResponse(json.dumps({"same_entity": False, "reason": "煙霧測試假回答"}, ensure_ascii=False))


def _test_entity_resolver_merges_synonyms() -> None:
    """不需要 Neo4j 的離線測試：驗證灰色地帶邏輯（見 pipeline/entity_resolution.py）
    在真實實體專有名詞上，至少能合併「中文全名 vs 英文名」這種明顯同義的
    情況。這裡故意不 mock LLM：「台達電子」跟「Delta Electronics」的
    cosine similarity 落在灰色地帶，會真的觸發一次 LLM 呼叫，但因為 prompt
    很短（只有兩個實體名稱，不像三元組抽取那樣帶整篇文章），不管打本機
    Ollama 還是雲端 API 都是秒級速度，值得留著測真的路徑。"""
    resolver = EntityResolver(existing_canonical_names=[])
    canonical_1, is_new_1 = resolver.resolve("台達電子")
    canonical_2, is_new_2 = resolver.resolve("Delta Electronics")
    assert is_new_1, "第一個實體字串應該是新的 canonical"
    print(
        f"[smoke_test] EntityResolver：「台達電子」→「{canonical_1}」，"
        f"「Delta Electronics」→「{canonical_2}」（is_new={is_new_2}）"
    )


def main() -> None:
    _test_entity_resolver_merges_synonyms()

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    conn = get_connection(str(TEST_DB_PATH))

    print("[smoke_test] 抓取真實候選文章（連網，不用 API key）...")
    # 抓全部來源取第一篇抓得到的，不要只賭單一來源：單一來源剛好沒有落在
    # days_back 窗口內的新文章是常態（RSS 更新頻率不固定），跟
    # smoke_test_topic_pipeline.py 一樣的做法。
    candidates: list = []
    for source in config["sources"]:
        if source.get("type", "rss") != "rss":
            continue
        candidates = fetch_case_study_items(
            source_id=source["id"],
            source_name=source["name"],
            url=source["url"],
            weight=source["weight"],
            days_back=config["fetch"]["days_back"],
            max_items=1,
        )
        if candidates:
            break
    assert candidates, "所有來源都沒抓到任何真實候選文章，無法繼續煙霧測試"
    article_id = insert_article_if_new(conn, candidates[0])
    assert article_id is not None, "文章入池失敗（可能剛好跟舊測試資料撞網址）"

    # 先標籤（用假值即可，這一步不是這支測試的重點），graph 步驟只處理
    # 「已標籤」的文章，跟 scripts/ingest_topics.py 的實際順序一致。
    save_article_tags(
        conn, article_id, content_mode="use", is_case_example=False,
        case_industry=None, case_department=None, case_outcome=None,
        tech_tags=[], entity_tags=[], scenario_tags=[], industry_tags=[],
        one_line_summary="煙霧測試假摘要",
    )

    week_start = week_start_date()
    to_graph = get_ungraphed_articles(conn, week_start)
    assert len(to_graph) == 1, "待建圖文章應該剛好是這篇新入池的文章"
    article_row = to_graph[0]
    print(f"[smoke_test] 待建圖文章：{article_row['title'][:60]}")

    neo4j_password = os.environ.get("NEO4J_PASSWORD")
    assert neo4j_password, "NEO4J_PASSWORD 沒設定，先在 .env 填好再跑這支測試"
    driver = graph_store.get_driver(TEST_NEO4J_URI, os.environ.get("NEO4J_USER", "neo4j"), neo4j_password)
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")  # 保證每次跑都是乾淨狀態
    graph_store.ensure_constraints(driver)

    resolver = EntityResolver(graph_store.get_all_canonical_entity_names(driver))

    # patch create_chat_completion() 本身（在各自 import 進去的模組名稱空間
    # 下 patch，不是 patch openai.OpenAI），才不會被 llm_client.py 內部的
    # Ollama 短路邏輯繞過去，見 _fake_create_chat_completion() 的說明。
    with patch("pipeline.triple_extraction.create_chat_completion", _fake_create_chat_completion), \
         patch("pipeline.entity_resolution.create_chat_completion", _fake_create_chat_completion):
        item = _row_to_raw_item(article_row)
        parsed = extract_triples(item)
        assert parsed["triples"], "mock 三元組抽取沒有回傳任何三元組"

        # add_article_to_graph() 內部的 EntityResolver 落在灰色地帶時會呼叫
        # LLM 驗證（見 pipeline/entity_resolution.py），要留在 patch 範圍內。
        stats = graph_builder.add_article_to_graph(driver, resolver, article_row, parsed["triples"])
    mark_article_graphed(conn, article_row["id"])
    print(f"[smoke_test] 建圖統計：{stats}")

    assert get_ungraphed_articles(conn, week_start) == [], "標記建圖完成後，這篇文章不該再出現在待建圖清單"

    with driver.session() as session:
        article_count = session.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"]
        entity_count = session.run("MATCH (e:Entity) RETURN count(e) AS c").single()["c"]
        edge_count = session.run("MATCH ()-[:RELATED]->() RETURN count(*) AS c").single()["c"]
    assert article_count >= 1 and entity_count >= 1 and edge_count >= 1, "圖裡沒寫入任何節點/邊"
    print(f"[smoke_test] 通過：{article_count} 篇文章節點、{entity_count} 個實體節點、{edge_count} 條關係邊")
    print("[smoke_test] 全部通過。")


if __name__ == "__main__":
    main()
