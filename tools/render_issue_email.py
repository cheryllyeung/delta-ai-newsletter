"""把一期日報渲染成 EDM 信件的 HTML（templates/email_issue.html.jinja）。

只渲染、不寄送。寄送（Outlook 自動化）是之後的另一支，先讓排版可以
被人工檢視。輸出到 runs/email_preview_<日期>.html，用瀏覽器開即可預覽
（實際寄出後在 Outlook 裡的樣子會更保守，但版型一致）。

用法：
    python -m tools.render_issue_email               # 最新一期
    python -m tools.render_issue_email --issue-id 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.edm_tags import is_primary, tags_for_topic
from pipeline.text_emphasis import emphasize_numbers
from pipeline.issue_tldr import dimension_by_topic, dimension_groups, tldr_display_groups
from pipeline.topic_db import get_connection

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "http://TWTP1NB3422.delta.corp:8002"


def _with_emphasis(sections):
    """把內文的數字標成粗體（pipeline/text_emphasis.py）。轉義與加粗都在
    程式端做，不讓模型輸出 HTML。"""
    if not sections:
        return sections
    out = []
    for sec in sections:
        body = sec.get("body") or sec.get("text") or ""
        out.append({**sec, "body_html": Markup(emphasize_numbers(body))})
    return out


def _build_faq(conn, config, issue, published_count: int) -> list[dict]:
    """信底常見問題（2026-09-30 加）。

    原本想把編輯邏輯寫成一整段說明或另附 PDF，兩種都試過：整段說明像把
    內部文件夾進刊物，PDF 則沒人會為了看一份日報去開附件。改成五個問答
    放信底，讀者有疑問時往下看就有答案。

    數字一律現算，不寫死：今天掃了幾篇、刊出幾則、關注廠商近 30 天有幾家
    真的出現過。寫死的數字過兩天就是假的。
    """
    from pipeline.edm_tags import _mentions

    scanned = conn.execute(
        "SELECT count(*) c FROM articles WHERE date(fetched_at) = ?", (issue["issue_date"],)
    ).fetchone()["c"]
    floor = config["selection"]["daily"].get("min_topics_to_publish", 0)

    watchlist = config["edm"]["vendor_watchlist"]
    names = [v[0] for v in watchlist]
    rows = conn.execute(
        """SELECT title, content FROM articles
           WHERE discarded_at IS NULL AND published_at >= date(?, '-30 day')""",
        (issue["issue_date"],),
    ).fetchall()
    seen = 0
    for entry in watchlist:
        display, *aliases = entry
        if any(
            _mentions(a, (r["title"] or "") + "\n" + (r["content"] or ""))
            for r in rows
            for a in [display, *aliases]
        ):
            seen += 1

    def hl(text: str) -> str:
        """重點加底線（2026-09-30 使用者選底線，不要底色）。用 <u> 標籤而不是
        text-decoration：Outlook 以 Word 引擎渲染，<u> 一定吃，CSS 不一定。"""
        return f"<u>{text}</u>"

    def b(text: str) -> str:
        return f"<b>{text}</b>"

    return [
        {
            "q": f"今天為什麼只有 {published_count} 則？",
            "lead": f"我們不設固定則數，{hl('當天達標的全出')}。今天的實際情況是：",
            "points": [
                f"掃進來 {b(f'{scanned} 篇')}",
                "過品質關卡，新聞彙總型欄目、付費牆只有前兩段的片段、"
                f"正文少於 {b('200 字')}的都擋掉",
                f"選題後留下 {b(f'{published_count} 則')}",
            ],
            "tail": f"當天合格的報導{hl(f'少於 {floor} 則就不出刊')}。",
        },
        {
            "q": "分類是怎麼分的？",
            "lead": "我們分兩層：",
            "points": [
                f"{b('面向')}：每則歸到市場、技術、臨床、法規其中一個，"
                "上方導讀就是照這四類分區",
                f"{b('分區')}：指定來源或名單廠商的動態進「主要報導」，"
                "其他來源進「額外報導」",
            ],
            "tail": f"導讀列的是{hl('下方報導的原標題與序號')}，上下是同一份文字。",
        },
        {
            "q": "目前涵蓋哪些廠商？",
            "lead": f"名單上目前 {len(names)} 家：{'、'.join(names)}。",
            "points": [
                "名單上的廠商，不論動態出現在哪個來源都會被抓進主要報導",
                f"近 30 天實際出現在報導裡的有 {b(f'{seen} 家')}",
                "其餘幾家的動態還沒抓到，社群媒體是下一步要接的來源",
            ],
            "tail": f"我們的{hl('來源會繼續擴增')}。",
        },
        {
            "q": "怎麼判斷哪則重要？",
            "lead": "我們排序的標準是三件事：",
            "points": [
                f"{b('幾家媒體在報同一件事')}，版面上會標「N 家在報」",
                f"{b('是否涉及名單上的廠商')}，或檢測市場本身",
                f"{b('對決策的影響大小')}",
            ],
            "tail": f"報頭的焦點就是按這三項挑出的{hl('一到兩則')}，方便先看哪則。",
        },
        {
            "q": "主編觀察是怎麼寫的？",
            "lead": "市場、技術、法規、臨床四個面向各一塊。我們寫的是"
                    f"{hl('把當天這批放在一起才看得到')}的東西，通常是三種：",
            "points": [
                "幾則其實指向同一件事，而它們自己沒點出那是同一件事",
                "兩則放在一起會互相削弱，一則的賣點正好是另一則暴露的瓶頸",
                "幾則共用一個還沒被驗證的前提",
            ],
            "tail": f"這一欄{b('不重述上面的報導')}，內容是判斷與推測，"
                    f"提到的數字{hl('都來自當期原文')}。",
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", type=int, default=None)
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    conn = get_connection(str(ROOT / "data" / Path(config["database"]["path"]).name))

    if args.issue_id:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (args.issue_id,)).fetchone()
    else:
        issue = conn.execute("SELECT * FROM issues ORDER BY issue_date DESC, id DESC LIMIT 1").fetchone()
    if issue is None:
        print("[render_issue_email] 沒有任何一期可渲染。")
        sys.exit(1)

    tldr = None
    try:
        if issue["tldr_json"]:
            tldr = json.loads(issue["tldr_json"])
    except (KeyError, IndexError):
        pass
    # 每則的分類標示與上面導讀的分區用同一套面向（2026-09-30 使用者要求：
    # 上下標題與分類要統一，原本上面是市場／技術，下面是廠商動態／台灣動態，
    # 看起來像兩套分類）。舊期數沒有這份對應表，退回原本的 primary_tag。
    dims = dimension_by_topic(tldr)

    rows = conn.execute(
        "SELECT * FROM generated_topics WHERE issue_id = ? ORDER BY id", (issue["id"],)
    ).fetchall()
    articles = []
    for idx, r in enumerate(rows, 1):
        g = json.loads(r["generated_json"])
        # 摘要用卡片文案；台灣標記看打分（taiwan_industry 過 4 分就標）
        scores_row = conn.execute(
            "SELECT module_scores_json FROM topics WHERE id = ?", (r["topic_id"],)
        ).fetchone()
        tw = 0.0
        if scores_row and scores_row["module_scores_json"]:
            tw = json.loads(scores_row["module_scores_json"]).get("taiwan_industry", {}).get("score", 0)
        # 主要來源那一篇（連結指向它）＋幾家在報。2026-09-30 起 EDM 不放
        # 全文，標題與「看原文」都連原文網站，所以這裡要拿到來源名稱。
        src = conn.execute(
            """SELECT url, source_name FROM articles
               WHERE topic_id = ? AND discarded_at IS NULL
                 AND (gate_status IS NULL OR gate_status != 'excluded')
               ORDER BY published_at DESC LIMIT 1""",
            (r["topic_id"],),
        ).fetchone()
        source_count = conn.execute(
            """SELECT count(DISTINCT source_id) c FROM articles
               WHERE topic_id = ? AND discarded_at IS NULL
                 AND (gate_status IS NULL OR gate_status != 'excluded')""",
            (r["topic_id"],),
        ).fetchone()["c"]
        tag = dims.get(r["topic_id"]) or g.get("primary_tag", "其他")
        articles.append(
            {
                "num": f"{idx:02d}",
                "headline": g.get("chosen_headline", ""),
                "subhead": g.get("chosen_subhead", ""),
                "summary": (g.get("card_summary") or {}).get("text", ""),
                "primary_tag": tag,
                "is_taiwan": tw >= 4,
                "url": f"{SITE_URL}/issues/{issue['id']}/topics/{r['id']}",
                "source_url": src["url"] if src else None,
                "source_name": src["source_name"] if src else "原文",
                "source_count": source_count,
                # 標籤（廠商／產品／技術）與分區都經過原文比對，見
                # pipeline/edm_tags.py：模型抽的標籤若在原文找不到就丟掉。
                "tags": tags_for_topic(conn, r["topic_id"], config),
                "is_primary": is_primary(conn, r["topic_id"], config),
                "needs_review": bool(r["needs_review"]),
                # 2026-09-18 信件自包含：完整內容直接放進信裡，收件人不用
                # 連回筆電上的網頁伺服器也讀得到（筆電關機連結就死）。
                # 自檢信心偏低（needs_review）的不放全文，只給摘要與原文
                # 連結（使用者定的規則：自檢程度低就不該放進去）。
                "sections": [] if r["needs_review"] else (g.get("sections") or []),
                "stats": [] if r["needs_review"] else (g.get("stats") or []),
                "delta_insight": None if r["needs_review"] else g.get("delta_insight"),
            }
        )

    # 主要報導排前面（NBDMD 清單來源或主角是 watchlist 廠商），其餘進額外
    # 報導。兩區各自維持原本的排序（選題分數高到低）。
    primary_articles = [a for a in articles if a["is_primary"]]
    extra_articles = [a for a in articles if not a["is_primary"]]
    # 導讀的編號必須是版面上實際印的序號（主要報導先排），不是資料庫順序。
    ordered = primary_articles + extra_articles
    for n, a in enumerate(ordered, 1):
        a["display_num"] = n
    groups = dimension_groups(
        [{"num": a["display_num"], "dimension": a["primary_tag"], "title": a["headline"]}
         for a in ordered if a["primary_tag"] in ("市場", "技術", "臨床", "法規")]
    ) or tldr_display_groups(tldr)

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    html = env.get_template("email_issue.html.jinja").render(
        tldr=tldr,
        tldr_groups=groups,
        editorial_sections=_with_emphasis((tldr or {}).get("editorial_sections")),
        faq=_build_faq(conn, config, issue, len(articles)),
        newsletter_name=config["newsletter"]["name"],
        issue_title=f"{config['newsletter']['name']}（{issue['issue_date']}）",
        issue_date=issue["issue_date"],
        issue_no=issue["id"],
        articles=articles,
        primary_articles=primary_articles,
        extra_articles=extra_articles,
        site_url=SITE_URL,
    )

    out = ROOT / "runs" / f"email_preview_{issue['issue_date']}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[render_issue_email] 已輸出：{out}（{len(articles)} 則）")


if __name__ == "__main__":
    main()
