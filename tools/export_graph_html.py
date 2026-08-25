"""把 Neo4j 裡的知識圖譜匯出成一個獨立的互動 HTML。

存在的理由是分享：整套系統跑在筆電上，Neo4j Browser 只有 localhost 進得去，
沒辦法給主管或同事看。匯出成單一 HTML 之後可以直接 email，對方點開就能拖拉
縮放，不用連任何東西、不用裝任何東西（vis.js 內嵌進檔案，離線也能開）。

預設會過濾掉大部分節點，這是刻意的。2026-08-13 實測圖的結構：

    493 個實體裡有 327 個（66%）只有一條連結
    51 個互不相連的碎片，其中 25 個只是孤立的一對
    平均連結數 2.06

全部畫出來會是一團看不懂的毛球，那些只出現一次的實體對「看出關聯」沒有任何
貢獻。預設只留連結數 >= 3 且落在最大連通塊裡的節點，剩下約 60 到 70 個，
才看得出重心在哪。要看全部就用 --min-degree 1 --all-components。

只畫 Entity 之間的關係，不畫 Article/Source 節點：那兩種是 provenance 用的，
畫進去會讓圖變成一堆星狀結構。要查某條邊出自哪篇文章就回 Neo4j Browser 查。

用法：
    python -m tools.export_graph_html
    python -m tools.export_graph_html --min-degree 1 --all-components
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import networkx as nx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from pipeline import graph_store

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "knowledge_graph.html"

# 節點類型上色，對應 prompts/triple_extraction.md 限定的五類。
_TYPE_COLORS = {
    "組織": "#2a5db0",
    "產品": "#e8833a",
    "技術": "#3f9c6d",
    "人物": "#a04ea0",
    "地區": "#8a8f98",
}
_DEFAULT_COLOR = "#c0c5cc"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-degree", type=int, default=3,
        help="只畫連結數達到這個值的實體，預設 3。訂 1 會把只出現一次的孤點也畫進去，圖會變成毛球。",
    )
    parser.add_argument(
        "--all-components", action="store_true",
        help="連沒跟主體相連的碎片也畫。預設只畫最大的那一塊，因為散落的碎片在畫面上只是雜訊。",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="輸出的 HTML 路徑")
    return parser.parse_args()


def fetch_graph(driver):
    with driver.session() as session:
        edges = [
            (r["a"], r["b"], r["rel"])
            for r in session.run(
                """MATCH (a:Entity)-[r:RELATED]->(b:Entity)
                   RETURN a.canonical_name AS a, b.canonical_name AS b, r.relation AS rel"""
            )
        ]
        types = {
            r["n"]: r["t"]
            for r in session.run("MATCH (e:Entity) RETURN e.canonical_name AS n, e.entity_type AS t")
        }
    return edges, types


def main() -> None:
    args = parse_args()
    uri = os.environ.get("NEO4J_URI")
    if not uri:
        print("[export_graph_html] NEO4J_URI 未設定，無法匯出。")
        return

    driver = graph_store.get_driver(
        uri, os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")
    )
    edges, types = fetch_graph(driver)
    if not edges:
        print("[export_graph_html] 圖裡還沒有實體關係，先跑 tools/backfill_graph_extraction.py。")
        return

    # 同一對實體可能因為多篇文章而有多條關係，合併成一條、把關係詞收集起來，
    # 不然畫面上會有一堆重疊的平行邊。
    merged: dict[tuple[str, str], set[str]] = {}
    for a, b, rel in edges:
        merged.setdefault((a, b), set()).add(rel or "相關")

    graph = nx.Graph()
    graph.add_edges_from(merged)
    total_nodes = graph.number_of_nodes()

    keep = {n for n, d in graph.degree() if d >= args.min_degree}
    graph = graph.subgraph(keep).copy()
    if not args.all_components and graph.number_of_nodes():
        largest = max(nx.connected_components(graph), key=len)
        graph = graph.subgraph(largest).copy()

    if not graph.number_of_nodes():
        print(f"[export_graph_html] 門檻 {args.min_degree} 之後沒有節點剩下，調低 --min-degree 再試。")
        return

    degree = dict(graph.degree())
    print(f"[export_graph_html] 全圖 {total_nodes} 個實體，畫出 {graph.number_of_nodes()} 個"
          f"（連結數 >= {args.min_degree}"
          f"{'' if args.all_components else '、且在最大連通塊內'}）、{graph.number_of_edges()} 條關係")

    from pyvis.network import Network

    # cdn_resources="in_line"：把 vis.js 整包寫進 HTML，寄出去之後在沒有網路或
    # 擋外連的環境也打得開。
    net = Network(height="92vh", width="100%", bgcolor="#f2f5f8", font_color="#1c2b38",
                  cdn_resources="in_line", notebook=False)
    # 節點少的時候用比較鬆的斥力，讓群落之間拉得開、標籤不會疊在一起。
    net.barnes_hut(gravity=-26000, central_gravity=0.15, spring_length=210, spring_strength=0.02)

    for name in graph.nodes():
        d = degree[name]
        node_type = types.get(name) or "未分類"
        net.add_node(
            name,
            label=name if d >= max(args.min_degree + 1, 4) else " ",
            title=f"{name}｜{node_type}｜連結數 {d}",
            color=_TYPE_COLORS.get(node_type, _DEFAULT_COLOR),
            size=10 + min(d, 28) * 1.5,
            font={"size": 16 + min(d, 20)},
        )
    for a, b in graph.edges():
        rels = merged.get((a, b)) or merged.get((b, a)) or {"相關"}
        # 關係詞放 tooltip 不放 label：500 條邊各掛一個標籤會把畫面蓋滿。
        net.add_edge(a, b, title="、".join(sorted(rels)), color="#b7c0ca", width=1)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(out), notebook=False, open_browser=False)

    # pyvis 的樣板會連 CDN 抓 Bootstrap（它選單面板用的，我們沒開），拿掉才不會
    # 在擋外連的環境卡住等逾時。順便塞一個圖例進去。
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:.35em;margin-right:1em">'
        f'<span style="width:.8em;height:.8em;border-radius:50%;background:{c}"></span>{t}</span>'
        for t, c in _TYPE_COLORS.items()
    )
    header = (
        '<div style="font-family:system-ui,\'Microsoft JhengHei\',sans-serif;padding:.7em 1em;'
        'background:#1c2b38;color:#eef1f4;font-size:.85rem;display:flex;flex-wrap:wrap;'
        'gap:.5em 1.5em;align-items:center">'
        f'<strong>台達 AI 電子報｜知識圖譜</strong>'
        f'<span>{graph.number_of_nodes()} 個實體、{graph.number_of_edges()} 條關係'
        f'（已濾掉連結數少於 {args.min_degree} 的）</span>'
        f'<span style="margin-left:auto">{legend}</span>'
        '<span style="width:100%;opacity:.7">節點大小代表連結數，滑鼠移上去看類型與關係</span>'
        "</div>"
    )
    html = "\n".join(
        line for line in out.read_text(encoding="utf-8").splitlines()
        if "cdn.jsdelivr.net/npm/bootstrap" not in line
    ).replace("<body>", f"<body>{header}", 1)
    out.write_text(html, encoding="utf-8")

    print(f"[export_graph_html] 已產出：{out}（{out.stat().st_size / 1024:.0f} KB，可直接 email）")


if __name__ == "__main__":
    main()
