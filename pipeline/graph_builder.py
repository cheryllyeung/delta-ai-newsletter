"""把單篇文章的三元組抽取結果（實體字串尚未正規化）寫進 Neo4j：先用
EntityResolver 把每個實體字串併到既有 canonical 實體（或建立新的），
再寫 Article 節點、MENTIONS 邊、RELATED 邊。

跟 pipeline/triple_extraction.py（只負責 LLM 呼叫）分開，是同一種
tag_article() / save_article_tags() 的抽取跟持久化分工方式。
"""
from __future__ import annotations

import sqlite3

from pipeline import graph_store
from pipeline.entity_resolution import EntityResolver


def add_article_to_graph(
    driver, resolver: EntityResolver, article_row: sqlite3.Row, triples: list[dict]
) -> dict:
    """回傳統計數字（新建/合併實體數、寫入邊數），方便呼叫端印 log。"""
    graph_store.upsert_source_node(
        driver, article_row["source_id"], article_row["source_name"], article_row["tier"]
    )
    graph_store.upsert_article_node(
        driver, article_row["id"], article_row["title"], article_row["url"],
        article_row["published_at"], article_row["source_name"],
        engagement_raw=article_row["engagement_raw"], engagement_source=article_row["engagement_source"],
    )
    graph_store.link_published(driver, article_row["source_id"], article_row["id"])

    new_entities = merged_entities = edges = 0
    linked_this_article: set[str] = set()

    for triple in triples:
        subject_canonical, subject_is_new = resolver.resolve(triple["subject"])
        object_canonical, object_is_new = resolver.resolve(triple["object"])

        for raw_name, canonical, is_new, entity_type in (
            (triple["subject"], subject_canonical, subject_is_new, triple.get("subject_type")),
            (triple["object"], object_canonical, object_is_new, triple.get("object_type")),
        ):
            graph_store.upsert_entity_node(driver, canonical, entity_type, alias=raw_name)
            new_entities += int(is_new)
            merged_entities += int(not is_new)
            if canonical not in linked_this_article:
                graph_store.link_mention(driver, article_row["id"], canonical)
                linked_this_article.add(canonical)

        graph_store.add_relation(
            driver, subject_canonical, triple["relation"], object_canonical, article_row["id"]
        )
        edges += 1

    return {"new_entities": new_entities, "merged_entities": merged_entities, "edges": edges}
