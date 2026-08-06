"""話題式週報的知識圖譜：Neo4j 儲存 OpenIE 三元組與文章-實體 provenance。

跟 pipeline/vector_store.py 的定位一樣：先用最簡單能跑的方式接上本機
docker-compose 開的單機 Neo4j，之後要多人共用/上 VM，只要把 NEO4J_URI
換成遠端位置，其他程式碼不用改。

Neo4j 是 multigraph：同一對節點之間可以有多條同類型的關係邊，這裡刻意
不把「相同主客體、不同文章來源」的關係合併成一條邊，每次抽取到的三元組
都各自寫一條邊、帶自己的 article_id，才能保留完整 provenance（同一組
主客體如果被不同文章講出不同關係，也都各自留下來，不互相覆蓋）。

Article 節點的 id 直接沿用 pipeline/topic_db.py 裡 articles 表的自增 id，
跟 Qdrant point id 是同一把 key 的邏輯，三邊（SQLite/Qdrant/Neo4j）互相
對得起來，不用另外維護對應表。

Schema：(Source)-[:PUBLISHED]->(Article)-[:MENTIONS]->(Entity)。tier（核心層/
訊號層/深度層/垂直/case）是來源的屬性，放在 Source 節點上，不冗餘複製到
MENTIONS 邊上——tier 定義之後如果改，只要動 Source 節點，不用回填所有邊。
engagement_raw/engagement_source 是單篇文章的快照數字（例如某篇 HN 貼文
當時的 points），放在 Article 節點上，跟 tier 這種來源層級的屬性分開存。
"""
from __future__ import annotations

from neo4j import Driver, GraphDatabase

_drivers: dict[str, Driver] = {}


def get_driver(uri: str, user: str, password: str) -> Driver:
    """同一個 uri 只建立一次 driver（driver 內部自己管理連線池）。"""
    if uri not in _drivers:
        _drivers[uri] = GraphDatabase.driver(uri, auth=(user, password))
    return _drivers[uri]


def ensure_constraints(driver: Driver) -> None:
    """Source.id、Article.id、Entity.canonical_name 各自唯一，MERGE 才能
    正確地「有就用、沒有就建」，不會意外堆出重複節點。"""
    with driver.session() as session:
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Source) REQUIRE s.id IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (a:Article) REQUIRE a.id IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.canonical_name IS UNIQUE")


def upsert_source_node(driver: Driver, source_id: str, source_name: str, tier: str) -> None:
    """tier（核心層/訊號層/深度層/垂直/case）放在來源節點上，不是文章或
    MENTIONS 邊上：tier 是「這個來源是什麼性質」的屬性，不是單篇文章或
    單次提及的屬性。"""
    with driver.session() as session:
        session.run(
            "MERGE (s:Source {id: $id}) SET s.name = $name, s.tier = $tier",
            id=source_id, name=source_name, tier=tier,
        )


def link_published(driver: Driver, source_id: str, article_id: int) -> None:
    with driver.session() as session:
        session.run(
            """MATCH (s:Source {id: $source_id}), (a:Article {id: $article_id})
               MERGE (s)-[:PUBLISHED]->(a)""",
            source_id=source_id, article_id=article_id,
        )


def upsert_article_node(
    driver: Driver,
    article_id: int,
    title: str,
    url: str,
    published_at: str,
    source_name: str,
    engagement_raw: float | None = None,
    engagement_source: str | None = None,
) -> None:
    """engagement_raw/engagement_source 只在來源本身有真實熱度數字時才有值
    （例如 HN points、Reddit upvotes），其他來源（核心層 RSS、arXiv 的
    關鍵字命中分數）語意不是熱度，不該塞進這兩個欄位，維持 NULL。"""
    with driver.session() as session:
        session.run(
            """MERGE (a:Article {id: $id})
               SET a.title = $title, a.url = $url, a.published_at = $published_at,
                   a.source_name = $source_name,
                   a.engagement_raw = $engagement_raw, a.engagement_source = $engagement_source""",
            id=article_id, title=title, url=url, published_at=published_at, source_name=source_name,
            engagement_raw=engagement_raw, engagement_source=engagement_source,
        )


def upsert_entity_node(
    driver: Driver, canonical_name: str, entity_type: str | None, alias: str | None = None
) -> None:
    """alias 是文章原文裡實際出現的寫法（例如「台達電」），跟 canonical_name
    不同就累積進 aliases 屬性，方便之後在 Neo4j Browser 上稽核「這個節點
    合併了哪些原始寫法」。"""
    with driver.session() as session:
        session.run(
            """MERGE (e:Entity {canonical_name: $canonical_name})
               ON CREATE SET e.entity_type = $entity_type, e.aliases = []
               WITH e
               FOREACH (_ IN CASE WHEN $alias IS NOT NULL AND $alias <> $canonical_name
                                        AND NOT $alias IN e.aliases
                                   THEN [1] ELSE [] END |
                   SET e.aliases = e.aliases + $alias)""",
            canonical_name=canonical_name, entity_type=entity_type, alias=alias,
        )


def link_mention(driver: Driver, article_id: int, canonical_name: str) -> None:
    with driver.session() as session:
        session.run(
            """MATCH (a:Article {id: $article_id}), (e:Entity {canonical_name: $canonical_name})
               MERGE (a)-[:MENTIONS]->(e)""",
            article_id=article_id, canonical_name=canonical_name,
        )


def add_relation(driver: Driver, subject: str, relation: str, obj: str, article_id: int) -> None:
    with driver.session() as session:
        session.run(
            """MATCH (s:Entity {canonical_name: $subject}), (o:Entity {canonical_name: $obj})
               CREATE (s)-[:RELATED {relation: $relation, article_id: $article_id}]->(o)""",
            subject=subject, relation=relation, obj=obj, article_id=article_id,
        )


def get_all_canonical_entity_names(driver: Driver) -> list[str]:
    """供 pipeline/entity_resolution.py 的 EntityResolver 初始化用：把目前
    圖裡已存在的 canonical 實體名稱全部撈出來當種子，新一輪 ingest 才不會
    把已經穩定存在的實體重新改名（改名會讓舊邊對不上新 canonical，等於
    斷掉 provenance）。"""
    with driver.session() as session:
        result = session.run("MATCH (e:Entity) RETURN e.canonical_name AS name")
        return [record["name"] for record in result]
