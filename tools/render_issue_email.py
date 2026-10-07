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


def _feedback_link(config: dict, issue_date: str, num: int, headline: str) -> str:
    """每則的「回報問題」連結（2026-10-07 加）。

    Outlook 不能放表單（不執行 JavaScript、form 會被剝除），所以用 mailto：
    點一下就開一封填好期數、則號與問題類型的信，讀者只要留下一項並補一句話。
    讀者本來幾乎不會主動回信，門檻降低才有機會拿到逐則的回饋。

    主旨與內文都要過 URL 編碼，中文與換行不編碼的話 Outlook 會把連結截斷。
    """
    from urllib.parse import quote

    to = config.get("edm", {}).get("feedback_email", "")
    if not to:
        return ""
    subject = f"[日報回饋] {issue_date} 第 {num:02d} 則"
    body = chr(10).join(
        [
            f"則號與標題：{num:02d} {headline}",
            "",
            "問題類型（請留下一項，其餘刪掉）：",
            "  摘要與原文對不上",
            "  數字或指標有誤",
            "  標題誤導",
            "  分類或面向不對",
            "  其他",
            "",
            "具體說明：",
            "",
        ]
    )
    return f"mailto:{to}?subject={quote(subject)}&body={quote(body)}"


def _build_faq(conn, config, issue, published_count: int, watchlist_hits: int) -> list[dict]:
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

    # 兩區的來源名稱從設定檔取，不寫死：來源清單會隨時增減，寫死的清單
    # 遲早跟實際抓的不一樣（2026-09-30 使用者要求信裡講明額外報導的來源）。
    primary_ids = set(config["edm"]["primary_source_ids"])
    primary_names = [s.get("name") or s["id"] for s in config["sources"] if s["id"] in primary_ids]
    other_names = [s.get("name") or s["id"] for s in config["sources"] if s["id"] not in primary_ids]

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
        """重點用藍字（2026-09-30 定案，先後試過淡金底色與底線都不好看）。
        深藍配米白紙底不刺眼，也跟信裡其他的金與墨色分得開。"""
        return f'<span style="color:#1f4e79; font-weight:700;">{text}</span>'

    def b(text: str) -> str:
        return f"<b>{text}</b>"

    return [
        {
            "q": f"今天為什麼只有 {published_count} 則？",
            "lead": "則數每天都不一樣，我們不湊到固定數量。今天是這樣來的：",
            "points": [
                f"掃進來 {b(f'{scanned} 篇')}",
                "過品質關卡，新聞彙總型欄目、付費牆只有前兩段的片段、"
                f"正文少於 {b('200 字')}的都擋掉",
                f"選題後留下 {b(f'{published_count} 則')}",
            ],
            "tail": f"合格的報導{b(f'不到 {floor} 則')}時，"
                    f"{hl('當天將不出刊')}，併入下一期。",
        },
        {
            "q": "分類是怎麼分的？",
            "lead": "每則歸到市場、技術、臨床、法規其中一個面向，上下都照這四類排。",
            "points": [
                f"{b('面向分區')}：導讀與下方報導用同一套順序與序號，點導讀會跳到那一則",
                f"{b('研究預印本另成一區')}：來自 bioRxiv 這類預印本平台，"
                "尚未經同儕審查，與正式發表分開呈現",
                f"{b('來源範圍')}：指定的 {len(primary_names)} 個來源加上名單廠商的官方管道，"
                f"另有 {len(other_names)} 個產業媒體與期刊來源",
            ],
            "tail": "",
        },
        {
            "q": "目前涵蓋哪些廠商？",
            "lead": f"名單上這 {len(names)} 家：{'、'.join(names)}。抓的情況分三種：",
            "points": [
                "名單上的廠商不論動態出現在哪個來源，我們都會收進來",
                f"近 30 天真的出現在報導裡的有 {b(f'{seen} 家')}",
                f"其他幾家目前{hl('還抓不到')}，我們正在擴充來源，社群媒體是下一步",
                f"本期與名單廠商直接相關的有 {b(f'{watchlist_hits} 則')}",
            ],
            # 這題不收尾：第三點已經把現況與下一步講完，再補一句只是突兀
            # （2026-09-30 使用者指出）。
            "tail": "",
        },
        {
            "q": "怎麼判斷哪則重要？",
            "lead": "排序看三件事：",
            "points": [
                f"{b('幾家媒體在報同一件事')}，版面上會標「N 家在報」",
                f"{b('有沒有涉及名單上的廠商')}，或檢測市場本身",
                f"{b('對決策的影響有多大')}",
            ],
            "tail": f"日報最上面那句「焦點」，就是這三項裡{hl('最突出的一到兩則')}。",
        },
        {
            "q": "主編觀察是怎麼寫的？",
            "lead": "四塊分別從市場、技術、法規、臨床看。每則報導上面已經有摘要，"
                    "這一欄再往上一層，寫幾則放在一起才看得出來的事，通常是這幾種：",
            "points": [
                "幾則其實在講同一件事，而它們自己沒點出來",
                "兩則放在一起會互相削弱，一則的賣點正好是另一則卡住的地方",
                "幾則都建立在同一個還沒被驗證的前提上",
            ],
            "tail": f"這一欄希望提供同仁一個{hl('跨則的判斷視角')}，"
                    "內容屬於推測，提到的數字都來自當期原文。",
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

    from pipeline.edm_tags import vendor_tags

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
        # 發布日期與預印本標記（2026-09-30 加）：主管實查時指出版面看不出
        # 新鮮度與證據等級，八則其實都是前一天發布的，其中三則是預印本。
        pub = conn.execute(
            """SELECT max(published_at) p FROM articles
               WHERE topic_id = ? AND discarded_at IS NULL
                 AND (gate_status IS NULL OR gate_status != 'excluded')""",
            (r["topic_id"],),
        ).fetchone()["p"]
        is_preprint = bool(
            conn.execute(
                """SELECT count(*) c FROM articles
                   WHERE topic_id = ? AND discarded_at IS NULL
                     AND (gate_status IS NULL OR gate_status != 'excluded')
                     AND (source_id LIKE '%biorxiv%' OR source_id LIKE '%medrxiv%'
                          OR url LIKE '%biorxiv%' OR url LIKE '%medrxiv%')""",
                (r["topic_id"],),
            ).fetchone()["c"]
        )
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
                "published_date": (pub or "")[:10],
                "feedback_url": _feedback_link(
                    config, issue["issue_date"], idx, g.get("chosen_headline", "")
                ),
                "is_preprint": is_preprint,
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
    # 當期有幾則真的跟名單上的廠商有關（2026-09-30 主管實查：整期 0 則，
    # 而信底又列了 26 家，讀者會以為名單跟當天有關）。
    watchlist_hits = 0
    for r in rows:
        arts = conn.execute(
            """SELECT title, content FROM articles
               WHERE topic_id = ? AND discarded_at IS NULL
                 AND (gate_status IS NULL OR gate_status != 'excluded')""",
            (r["topic_id"],),
        ).fetchall()
        if any(
            vendor_tags((a["title"] or "") + "\n" + (a["content"] or ""), config) for a in arts
        ):
            watchlist_hits += 1

    # 預印本自成一區（2026-09-30 主管實查：八則裡三則預印本，跟廠商動態
    # 並列在同一個權重上不對）。研究預印本無論主角是誰都歸這一區，選題端
    # 另外設了每期上限（config 的 tier_cap.preprint）。
    preprint_articles = [a for a in articles if a["is_preprint"]]
    rest = [a for a in articles if not a["is_preprint"]]

    # 2026-10-07 改成按面向分區，不再分主要與額外：主管指出讀者不會特別去
    # 分辨哪些來自額外來源，而原本「主要／額外」的排法讓導讀的序號跳成
    # 01、02、04、07、09、03，從上往下讀要一直回頭。改成上下都用同一套
    # 面向分區之後，序號自然連續。
    # 預印本仍獨立成最後一區：證據等級與主題是兩個維度，混在一起會讓讀者
    # 誤以為預印本的結論跟期刊發表同級。
    dimension_order = ["市場", "技術", "臨床", "法規"]
    by_dimension = []
    for name in dimension_order:
        members = [a for a in rest if a["primary_tag"] == name]
        if members:
            by_dimension.append({"label": name, "articles": members})
    # 面向對不上四類的（舊期數的 primary_tag）歸到最後，不要默默消失
    others = [a for a in rest if a["primary_tag"] not in dimension_order]
    if others:
        by_dimension.append({"label": "其他", "articles": others})

    ordered = [a for group in by_dimension for a in group["articles"]] + preprint_articles
    for n, a in enumerate(ordered, 1):
        a["display_num"] = n
    # 導讀要跟下方的分區一模一樣，包含預印本那一組：先前只按面向分組，
    # 預印本被按它的主題混進各組，導讀的序號就又跳成 01、02、03、04、09、05
    # （2026-10-07 實測）。目錄跟內容的順序必須一致。
    digest_groups = [
        {
            "label": group["label"],
            "entries": [f"{a['display_num']:02d}　{a['headline']}" for a in group["articles"]],
        }
        for group in by_dimension
    ]
    if preprint_articles:
        digest_groups.append(
            {
                "label": "研究預印本",
                "entries": [
                    f"{a['display_num']:02d}　{a['headline']}" for a in preprint_articles
                ],
            }
        )
    groups = digest_groups or tldr_display_groups(tldr)

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    html = env.get_template("email_issue.html.jinja").render(
        tldr=tldr,
        tldr_groups=groups,
        editorial_sections=_with_emphasis((tldr or {}).get("editorial_sections")),
        faq=_build_faq(conn, config, issue, len(articles), watchlist_hits),
        watchlist_hits=watchlist_hits,
        newsletter_name=config["newsletter"]["name"],
        issue_title=f"{config['newsletter']['name']}（{issue['issue_date']}）",
        issue_date=issue["issue_date"],
        issue_no=issue["id"],
        articles=articles,
        dimension_sections=by_dimension,
        preprint_articles=preprint_articles,
        site_url=SITE_URL,
    )

    out = ROOT / "runs" / f"email_preview_{issue['issue_date']}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[render_issue_email] 已輸出：{out}（{len(articles)} 則）")


if __name__ == "__main__":
    main()
