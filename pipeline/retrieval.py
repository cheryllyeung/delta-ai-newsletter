"""階段四：決定入選話題要拿哪幾篇文章當寫作素材。

## 2026-08-14 改寫，起因是「標題跟內容對不上」

同仁反映讀起來像是硬把幾個不相干的來源湊成一篇。回頭量了已經產出的 26 篇
文章共 63 篇素材，只有 29 篇（46%）真的屬於它自己的話題，其餘都是舊版本從
全池檢索補進來的。實際發生過的組合：

| 話題 | 被補進來的素材 | 結果 |
|---|---|---|
| 公用事業帳單系統阻礙費率創新 | Qwen 3.8-Max 跑分 | 成文標題變成講 AI 跑分，跟話題完全無關 |
| McLaren P1 換三倍電池 | 鈉電池、Cambricon 晶片、Moore Threads 上市 | 四篇各講各的 |
| Duke Energy 增資 100 億 | 一個叫 aigclink/geolook 的 GitHub repo | 兩件事沒有交集 |

舊做法是「用話題的代表文字，從**全池**撈 embedding top-20，reranker 重排取
top-5」，用意是話題只有一篇文章時補脈絡。問題在 reranker 判斷的是**主題層級
相關**（都在講 AI、都在講電力），不是**同一件事**。全池裡跟「電力」主題相關
的文章有幾十篇，撈出來的必然是別的事件。模型拿到五篇不同的事又被要求寫成
一篇，只能找共同點硬接，接出來就是同仁看到的那種文章。

min_relevance=0.3 沒有擋住，因為那些不相干的素材在主題層級的分數本來就不低。

## 現在的做法

素材主體＝這個話題自己的文章，沒有別的來源。

reranker 在這裡的角色從「去全池找東西」降級成「話題自己的文章太多時排個序」，
一般情況根本不會用到：191 個話題有 184 個底下只有一篇文章。

全池補充的程式碼留著但預設關閉（config 的 sources_for_writing.supplement），
理由寫在那裡。等聚類真的開始合併同事件的多家報導之後，「補脈絡」仍然有價值，
屆時要打開請先量「補進來的素材有多少比例真的同事件」，不要只看分數。

只有 gate_status='included' 的文章能當素材：只有一行標題的 signal_only 文章
拿去寫作，模型除了瞎掰沒有別的選擇。
"""
from __future__ import annotations

import sqlite3

from pipeline import embeddings, gates, reranker, vector_store
from pipeline.topic_db import get_articles_by_ids, get_articles_for_topic


def _query_text_for_topic(topic_row: sqlite3.Row, topic_articles: list[sqlite3.Row]) -> str:
    summaries = "\n".join(row["one_line_summary"] or "" for row in topic_articles)
    return f"{topic_row['representative_title']}\n{summaries}"


def _rerank_rows(query_text: str, rows: list[sqlite3.Row], top_k: int, min_score: float) -> list[sqlite3.Row]:
    doc_texts = [f"{row['title']}\n{row['content'][:2000]}" for row in rows]
    top_indices = reranker.rerank(query_text, doc_texts, top_k=top_k, min_score=min_score)
    return [rows[i] for i in top_indices]


def _fetch_supplements(
    conn: sqlite3.Connection,
    config: dict,
    query_text: str,
    exclude_ids: set[int],
    slots: int,
) -> list[sqlite3.Row]:
    """從全池找補充素材。只有設定打開時才會走到這裡，門檻刻意比舊值高很多。"""
    vector_cfg = config["vector_store"]
    supp_cfg = config["sources_for_writing"]["supplement"]

    query_vector = embeddings.encode_one(query_text)
    client = vector_store.get_client(vector_cfg["path"])
    hits = vector_store.search_similar(
        client, vector_cfg["collection"], query_vector, top_k=supp_cfg["retrieval_top_k"]
    )
    candidate_ids = [hit[0] for hit in hits if hit[0] not in exclude_ids]
    if not candidate_ids:
        return []

    rows_by_id = {row["id"]: row for row in get_articles_by_ids(conn, candidate_ids)}
    ordered = [rows_by_id[cid] for cid in candidate_ids if cid in rows_by_id]
    # 補充素材同樣只能是有內文的文章
    ordered = gates.substantive(ordered)
    if not ordered:
        return []

    return _rerank_rows(query_text, ordered, top_k=slots, min_score=supp_cfg["min_relevance"])


def retrieve_sources_for_topic(
    conn: sqlite3.Connection, config: dict, topic_row: sqlite3.Row
) -> tuple[list[sqlite3.Row], dict]:
    """回傳 (素材清單, 選材說明)。

    選材說明會被寫進 selection_trace，讓「這篇是拿哪幾篇寫的、有沒有從池裡
    補東西」在網頁上查得到，不用回頭翻 log。
    """
    cfg = config["sources_for_writing"]

    topic_articles = get_articles_for_topic(conn, topic_row["id"])
    own = gates.substantive(topic_articles)
    query_text = _query_text_for_topic(topic_row, topic_articles)

    detail = {
        "own_articles": len(own),
        "signal_only_articles": len(gates.signal_articles(topic_articles)),
        "supplements": 0,
        "single_source": gates.is_single_source(topic_articles),
    }

    max_sources = cfg["max_sources"]
    if len(own) > max_sources:
        # 話題自己的文章就超過上限，排個序取前幾篇。min_score 給 0：這些
        # 都是聚類判定為同一件事的文章，這裡只是排序不是過濾。
        sources = _rerank_rows(query_text, own, top_k=max_sources, min_score=0.0)
        detail["own_reranked_from"] = len(own)
    else:
        sources = list(own)

    supp_cfg = cfg["supplement"]
    if supp_cfg["enabled"] and len(sources) < supp_cfg["only_when_own_sources_below"]:
        slots = min(supp_cfg["max_supplements"], max_sources - len(sources))
        if slots > 0:
            exclude = {row["id"] for row in topic_articles}
            supplements = _fetch_supplements(conn, config, query_text, exclude, slots)
            sources.extend(supplements)
            detail["supplements"] = len(supplements)
            detail["supplement_ids"] = [row["id"] for row in supplements]

    detail["final_sources"] = len(sources)
    return sources, detail
