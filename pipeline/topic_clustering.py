"""階段二的核心邏輯：把新文章跟現有文章比對向量相似度，同事件多來源報導
合併成同一個「話題」（依 docs/prd/0731_PRD_v0.5.md 階段二設計）。

做法（PoC 階段刻意簡化，屬單一連結式的貪婪聚類，不是正式的聚類演算法，
之後要調準度可以在這裡換更嚴謹的方法，不用動呼叫端）：
每篇新文章 embedding 之後，去 Qdrant 找池裡向量最相似的幾篇既有文章。
相似度超過門檻的那些裡，最像的那篇決定新文章併入哪個話題；如果超過門檻的
文章分屬不同話題，代表新文章是把那幾個話題橋接起來的證據，那些話題會被
合併成一個。

## 2026-08-14 修掉的兩個洞

**話題永遠不會合併。** 舊版每篇新文章只跟最像的「那一篇」比（top_k=1），
併入那篇所屬的話題就結束。結果是同一件事的報導如果先後各自開了話題，之後
就算再像也各走各的：實測有兩個話題都在講同一款模型，跨話題相似度 0.738
已超過門檻 0.72，仍然是兩個話題。現在 top_k=5，超過門檻的既有文章分屬
多個話題時觸發合併（已出刊的話題除外，理由見 topic_db.merge_topics）。

**只有標題的文章會假合併。** 只有一行標題的短文本 embedding 會塌在一起：
實測把門檻降到 0.68 會出現一個 11 篇的團塊，成員是 8 個互不相關的 GitHub
repo 加兩則 ChatGPT 新聞加一則 Ford 新聞。所以比對的兩篇裡只要有一邊是
signal_only（只有標題），就改用更嚴的門檻（title_only_threshold），
不用一般門檻。

跟評分/標籤（pipeline/article_tagging.py、pipeline/module_scoring.py）完全
脫鉤：這裡只決定「這篇文章屬於哪個話題」，不管內容好壞。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline import embeddings, vector_store
from pipeline.gates import SIGNAL_ONLY
from pipeline.topic_db import (
    assign_article_to_topic,
    create_topic,
    get_topic_published_map,
    get_unclustered_articles,
    merge_topics,
)

# 找幾篇最相似的既有文章。1 就是舊行為（永遠不會觸發話題合併）。5 夠涵蓋
# 「同一件事已經有幾家報導、分散在幾個話題」的常見情況，再大隻是多比幾筆
# 低於門檻的。
_TOP_K = 5


def _embedding_text(row: sqlite3.Row) -> str:
    """拿標題+內文前段做 embedding，全文太長會拖慢速度、也稀釋標題訊號，
    新聞類文章的主題通常標題+前幾段就夠代表整篇在講什麼。"""
    return f"{row['title']}\n\n{row['content'][:2000]}"


def _is_signal_only(row) -> bool:
    try:
        return row["gate_status"] == SIGNAL_ONLY
    except (IndexError, KeyError):
        return False


def cluster_new_articles(
    conn: sqlite3.Connection,
    qdrant_path: str,
    collection: str,
    similarity_threshold: float,
    title_only_threshold: float | None = None,
) -> dict:
    """對 pool 裡還沒聚類的文章逐一跑聚類，回傳統計數字方便呼叫端印 log。

    title_only_threshold：比對的兩篇裡任一邊只有標題（signal_only）時改用的
    較嚴門檻。沒給就退回一律用 similarity_threshold（舊行為）。
    """
    rows = get_unclustered_articles(conn)
    if not rows:
        return {"processed": 0, "new_topics": 0, "merged": 0, "topics_merged": 0}

    client = vector_store.get_client(qdrant_path)
    vector_store.ensure_collection(client, collection, embeddings.embedding_dimension())

    # embedding 先整批算完再逐篇分配。逐篇呼叫 encode_one 的話，每篇都要付
    # 一次模型呼叫的固定開銷，實測在這台筆電上一篇要 20 秒以上；批次算把
    # 固定開銷攤掉。分配那段仍然要照順序逐篇做（後面的文章要跟前面的比），
    # 但那段只是查 Qdrant 跟寫 DB，毫秒級。
    all_vectors = embeddings.encode([_embedding_text(row) for row in rows])

    new_topics = 0
    merged = 0
    topics_merged = 0
    for row, vector in zip(rows, all_vectors):
        seen_at = datetime.now(timezone.utc).isoformat()
        row_is_signal = _is_signal_only(row)

        # 每個 hit 依「兩邊是否都有實質內文」各自決定門檻。hit 的 gate_status
        # 回 sqlite 查（payload 裡沒有存，舊資料也補不回來），一次最多 5 筆，
        # 查詢成本可忽略。
        hits = vector_store.search_similar(client, collection, vector, top_k=_TOP_K)
        matched_topic_ids: list[int] = []  # 依相似度由高到低，去重
        for hit_article_id, score, payload in hits:
            hit_topic_id = payload.get("topic_id")
            if hit_topic_id is None or hit_topic_id in matched_topic_ids:
                continue
            hit_row = conn.execute(
                "SELECT gate_status FROM articles WHERE id = ?", (hit_article_id,)
            ).fetchone()
            hit_is_signal = hit_row is not None and hit_row["gate_status"] == SIGNAL_ONLY
            threshold = similarity_threshold
            if title_only_threshold is not None and (row_is_signal or hit_is_signal):
                threshold = title_only_threshold
            if score >= threshold:
                matched_topic_ids.append(hit_topic_id)

        if matched_topic_ids:
            topic_id = matched_topic_ids[0]
            merged += 1
            # 新文章同時像好幾個話題：那幾個話題就是同一件事，合併。
            # 已出刊的話題不動（歷史記錄指著它們），從合併清單剔除；如果
            # 最像的那個本身已出刊，新文章還是掛進去（維持舊行為），只是
            # 不拿別的話題來併它。
            if len(matched_topic_ids) > 1:
                published = get_topic_published_map(conn, matched_topic_ids)
                mergeable = [t for t in matched_topic_ids if not published.get(t, False)]
                if not published.get(topic_id, False) and len(mergeable) > 1:
                    winner = mergeable[0]
                    for loser in mergeable[1:]:
                        moved = merge_topics(conn, winner_id=winner, loser_id=loser)
                        vector_store.set_topic_for_articles(client, collection, moved, winner)
                        topics_merged += 1
                    topic_id = winner
        else:
            topic_id = create_topic(conn, representative_title=row["title"], seen_at=seen_at)
            new_topics += 1

        assign_article_to_topic(conn, row["id"], topic_id, seen_at)
        vector_store.upsert_article(
            client,
            collection,
            article_id=row["id"],
            vector=vector,
            payload={"topic_id": topic_id, "title": row["title"], "url": row["url"]},
        )

    return {
        "processed": len(rows),
        "new_topics": new_topics,
        "merged": merged,
        "topics_merged": topics_merged,
    }
