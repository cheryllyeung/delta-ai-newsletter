"""把幾期網頁（serve_topics 出的 HTML）存成一份不需要伺服器就能開的靜態資料夾。

給「主管要看 HTML 但不想依賴這台筆電一直開著伺服器」這種場合用：每期把
issues/{id}（導讀頁）、issues/{id}/topics/{topic_id}、issues/{id}/trace
全部抓下來，各自存一個子資料夾，站內連結改寫成本機相對檔名，再產一份
跨期的列表頁當入口。整個資料夾丟給人就能直接雙擊 index.html 看，不需要
這台筆電開著伺服器。

前提：scripts.serve_topics 要先在本機跑起來（預設 http://localhost:8001）。

用法：
    python -m tools.export_static_issue 11              # 單期
    python -m tools.export_static_issue 6 7 8 9 11       # 多期，會多一份跨期列表頁
    python -m tools.export_static_issue 11 --out exports/0814
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"

_LIST_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{newsletter_name}（靜態離線版）</title>
<style>
  body {{ font-family: "PingFang TC", "Microsoft JhengHei", ui-sans-serif, sans-serif;
          background: #eef1f4; color: #1c2b38; margin: 0; padding: 2.5rem 1.5rem; }}
  .shell {{ max-width: 40rem; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 0.4rem; }}
  .note {{ color: #57707f; font-size: 0.85rem; margin: 0 0 2rem; line-height: 1.6; }}
  ul {{ list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.75rem; }}
  li a {{ display: block; background: #fff; border: 1px solid #dde3e8; border-radius: 6px;
          padding: 1rem 1.2rem; text-decoration: none; color: inherit; font-size: 1.05rem; }}
  li a:hover {{ border-color: #2a5db0; }}
</style>
</head>
<body>
<div class="shell">
  <h1>{newsletter_name}</h1>
  <p class="note">靜態離線版，不需要伺服器；點進單期後的話題連結、選題帳連結都可以正常用。</p>
  <ul>
{items}
  </ul>
</div>
</body>
</html>
"""


def _issue_meta(db_path: str, issue_id: int) -> tuple[list[int], str | None]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id FROM generated_topics WHERE issue_id = ? ORDER BY id", (issue_id,)
    ).fetchall()
    date_row = conn.execute(
        "SELECT issue_date FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()
    conn.close()
    return [r[0] for r in rows], (date_row[0] if date_row else None)


def _rewrite_links(html: str, issue_id: int, topic_ids: list[int]) -> str:
    html = html.replace(f'href="/issues/{issue_id}/trace"', 'href="trace.html"')
    for tid in topic_ids:
        html = html.replace(
            f'href="/issues/{issue_id}/topics/{tid}"', f'href="topic_{tid}.html"'
        )
    html = html.replace(f'href="/issues/{issue_id}"', 'href="index.html"')
    return html


def export_issue(issue_id: int, base_url: str, db_path: str, out_root: Path) -> str:
    """匯出一期到 out_root/<issue_date>/，回傳 issue_date（資料夾名）。"""
    topic_ids, issue_date = _issue_meta(db_path, issue_id)
    if not topic_ids:
        raise SystemExit(f"issue {issue_id} 沒有任何話題，先確認這一期有出過刊。")
    folder_name = issue_date or str(issue_id)
    out_dir = out_root / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    overview_resp = requests.get(f"{base_url}/issues/{issue_id}", timeout=30)
    overview_resp.raise_for_status()
    (out_dir / "index.html").write_text(
        _rewrite_links(overview_resp.text, issue_id, topic_ids), encoding="utf-8"
    )
    print(f"[export_static_issue] {folder_name}/index.html")

    for tid in topic_ids:
        resp = requests.get(f"{base_url}/issues/{issue_id}/topics/{tid}", timeout=30)
        resp.raise_for_status()
        (out_dir / f"topic_{tid}.html").write_text(
            _rewrite_links(resp.text, issue_id, topic_ids), encoding="utf-8"
        )
        print(f"[export_static_issue] {folder_name}/topic_{tid}.html")

    trace_resp = requests.get(f"{base_url}/issues/{issue_id}/trace", timeout=30)
    trace_resp.raise_for_status()
    (out_dir / "trace.html").write_text(
        _rewrite_links(trace_resp.text, issue_id, topic_ids), encoding="utf-8"
    )
    print(f"[export_static_issue] {folder_name}/trace.html")

    return folder_name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_ids", type=int, nargs="+")
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--out", default=None, help="輸出資料夾，預設 exports/<期數範圍>")
    args = parser.parse_args()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    db_path = config["database"]["path"]
    newsletter_name = config["newsletter"]["name"]

    if args.out:
        out_root = Path(args.out)
    elif len(args.issue_ids) == 1:
        out_root = Path("exports")
    else:
        out_root = Path("exports") / f"{min(args.issue_ids)}-{max(args.issue_ids)}"
    out_root.mkdir(parents=True, exist_ok=True)

    folder_names = [
        export_issue(issue_id, args.base_url, db_path, out_root) for issue_id in args.issue_ids
    ]

    if len(args.issue_ids) > 1:
        items = "\n".join(
            f'    <li><a href="{name}/index.html">{name}</a></li>' for name in folder_names
        )
        (out_root / "index.html").write_text(
            _LIST_PAGE_TEMPLATE.format(newsletter_name=newsletter_name, items=items),
            encoding="utf-8",
        )
        print(f"[export_static_issue] index.html（跨期列表頁）")

    print(f"[export_static_issue] 完成，共 {len(args.issue_ids)} 期 -> {out_root}")


if __name__ == "__main__":
    main()
