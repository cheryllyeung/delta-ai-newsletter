"""從報導內文裡抓出被引述的發言（2026-09-23 加，速報信的「觀點」那一段）。

速報的定位是「今天沒有累積到足以成一期的內容，但這幾件事有人在談」，所以
觀點那段要的是「誰說了什麼」，不是我們的詮釋。這支用規則抓引號段落，
不花 LLM 額度也不做改寫，翻譯另外一步（pipeline/quote_translate.py）。

刻意用規則而不是叫模型「整理各方觀點」：後者會把散在幾篇的說法融成一段
看似中立的敘述，讀者看不出哪句話是誰說的。引述就是引述，出處要跟著句子。

社群留言（tools/fetch_social_signals.py 存的 social_comments）走同一條
呈現路徑，但來源標的是討論串而不是媒體。
"""
from __future__ import annotations

import re
import sqlite3

# 英文引號段落 + 後面帶 said/says/told 的署名。中文報導的引號用「」，
# 署名習慣在前面（例如「X 表示：「……」」），所以分開兩條規則。
_EN_QUOTE = re.compile(
    r'[“"]([^”"]{40,300})[”"]\s*,?\s*(?:said|says|told|according to|noted|added)\s+([^.。]{2,60})',
    re.I,
)
# 署名在前的句型：X said, "……"。實測只抓「引言在前」那種，25 個話題裡
# 只命中 2 個，補上這條之後涵蓋率明顯提高。
_EN_QUOTE_LEAD = re.compile(
    r'([A-Z][\w.\'\- ]{2,40})(?:,[^,]{0,40})?,?\s+(?:said|says|told[^,]{0,30}|noted|added|explained)\s*[,:]?\s*[“"]([^”"]{40,300})[”"]'
)
_ZH_QUOTE = re.compile(r"([^。！？\n]{2,30}?)(?:表示|指出|說明|強調|認為|提到)[：:]?\s*[「“]([^」”]{20,200})[」”]")

_MAX_PER_ARTICLE = 2


def _clean_speaker(raw: str) -> str:
    """署名取到第一個逗號為止。原樣留著會變成「Jeon Woong Kang, PhD, an」
    這種抓到一半的句子。"""
    name = raw.strip().split(",")[0].strip()
    # 句型上偶爾會黏到前面的連接詞
    name = re.sub(r"^(?:and|but|while|that)\s+", "", name, flags=re.I)
    return name[:40]


def extract_quotes(content: str, limit: int = _MAX_PER_ARTICLE) -> list[dict]:
    """回傳 [{"speaker": str, "text": str, "lang": "en"|"zh"}]，抓不到就空清單。"""
    if not content:
        return []
    out: list[dict] = []
    for m in _EN_QUOTE.finditer(content):
        text, speaker = m.group(1).strip(), _clean_speaker(m.group(2))
        out.append({"speaker": speaker, "text": text, "lang": "en"})
        if len(out) >= limit:
            return out
    for m in _EN_QUOTE_LEAD.finditer(content):
        speaker, text = _clean_speaker(m.group(1)), m.group(2).strip()
        if any(q["text"] == text for q in out):
            continue
        out.append({"speaker": speaker, "text": text, "lang": "en"})
        if len(out) >= limit:
            return out
    for m in _ZH_QUOTE.finditer(content):
        speaker, text = m.group(1).strip(), m.group(2).strip()
        out.append({"speaker": speaker, "text": text, "lang": "zh"})
        if len(out) >= limit:
            break
    return out


def quotes_for_topic(conn: sqlite3.Connection, topic_id: int, limit: int = 2) -> list[dict]:
    """一個話題的引述：先看社群留言（網友觀點），不足再補報導裡的發言
    （專家觀點）。社群優先是因為那才是「大家怎麼看」，報導引述多半是
    當事人或機構自己的說法。"""
    quotes: list[dict] = []

    # 社群留言：tools/fetch_social_signals.py 抓的，表可能還不存在。
    try:
        rows = conn.execute(
            """SELECT c.author, c.text_original, c.text_zh, a.source_name, a.url
               FROM social_comments c JOIN articles a ON a.id = c.article_id
               WHERE a.topic_id = ? ORDER BY c.points DESC LIMIT ?""",
            (topic_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        quotes.append(
            {
                "speaker": f"網友 {r['author']}" if r["author"] else "網友",
                "text": r["text_original"],
                "text_zh": r["text_zh"],
                "kind": "社群",
                "source_name": r["source_name"],
                "url": r["url"],
            }
        )
        if len(quotes) >= limit:
            return quotes

    # 報導裡被引述的發言
    for a in conn.execute(
        """SELECT content, source_name, url FROM articles
           WHERE topic_id = ? AND discarded_at IS NULL AND gate_status = 'included'
           ORDER BY published_at DESC""",
        (topic_id,),
    ):
        for q in extract_quotes(a["content"]):
            quotes.append(
                {
                    "speaker": q["speaker"],
                    "text": q["text"],
                    "text_zh": q["text"] if q["lang"] == "zh" else None,
                    "kind": "報導引述",
                    "source_name": a["source_name"],
                    "url": a["url"],
                }
            )
            if len(quotes) >= limit:
                return quotes
    return quotes
