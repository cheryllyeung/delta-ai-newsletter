"""EDM 的標籤與分區（2026-09-30 加，NBDMD 需求）。

兩件事：

1. **標籤**：每則標上廠商、技術、產品，讓讀者一眼抓到重點。
2. **分區**：主要報導（NBDMD 清單來源，或主角是 watchlist 上的 26 家廠商）
   與額外報導（其餘來源）分開。

## 為什麼標籤不讓模型生成

標籤是讀者最先看到、也最容易被當成事實的東西。所以這裡的每個標籤都必須
**在原文字面上出現過**才會顯示：

- 廠商標籤：拿 config 的 vendor_watchlist（含同義寫法）去比對標題與內文
- 技術與產品標籤：讀抓取階段存下的 entity_tags／tech_tags／release_product，
  但一律再回原文驗證一次，比對不到的直接丟掉

模型仍然參與（tech_tags 是它抽的），但它的產出只能「被採用」不能「被相信」：
通不過原文比對的標籤不會出現在信裡。這是 2026-09-30 使用者要求「內容絕不
可有幻覺」時定下的作法。
"""
from __future__ import annotations

import json
import re
import sqlite3


def _mentions(term: str, haystack: str) -> bool:
    """term 是否在 haystack 裡出現。英文用詞邊界避免 clap 命中 clapperboard
    那種誤配，中文直接子字串比對（中文沒有詞邊界）。"""
    if not term or not haystack:
        return False
    if re.fullmatch(r"[\x00-\x7f]+", term):
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", haystack, re.I) is not None
    return term in haystack


def vendor_tags(text: str, config: dict) -> list[str]:
    """文章裡真的出現的 watchlist 廠商（回顯示名稱，去重、保持名單順序）。"""
    out: list[str] = []
    for entry in config["edm"]["vendor_watchlist"]:
        display, *aliases = entry
        if any(_mentions(a, text) for a in [display, *aliases]):
            out.append(display)
    return out


# 標籤的用途是「廠商／技術／產品」，這兩類要排除（2026-09-30 實測混進來）：
#   媒體名：MedCity News、STAT News 那類，是出處不是主題
#   人名：Giedrė Čepukaitytė、Robert F. Kennedy Jr. 那類，讀者要抓的是
#         公司與技術，不是受訪者
# 媒體名用池裡實際的來源名稱比對（不必手維護清單），人名用組織字尾判斷：
# 含有下列任一字樣的當組織，其餘「兩三個字的西文大寫詞」當人名丟掉。
_ORG_HINTS = (
    "inc", "ltd", "llc", "corp", "co.", "company", "institute", "university",
    "college", "hospital", "health", "bio", "genom", "labs", "laborator",
    "technolog", "pharma", "medical", "medicine", "center", "centre", "school",
    "foundation", "diagnostics", "sciences", "science", "systems", "group",
    "therapeutics", "capital", "ventures", "partners", "association", "society",
    "fda", "nih", "cdc", "ema", "who",
)


def _looks_like_person(tag: str) -> bool:
    """西文人名的粗略判斷：兩到四個以大寫開頭的詞、不含組織字樣。
    中文標籤不判（中文人名與公司名用這招分不開，寧可留著）。"""
    if not re.fullmatch(r"[A-Za-zÀ-ž.'\- ]{4,60}", tag):
        return False
    low = tag.lower()
    if any(h in low for h in _ORG_HINTS):
        return False
    words = [w for w in tag.split() if w]
    # 中間名縮寫（F.）與字尾（Jr.、Sr.、III）是人名的特徵，先摘掉再判，
    # 不然 Robert F. Kennedy Jr. 會因為含單字母而被當成技術代號。
    core = [w for w in words if not re.fullmatch(r"[A-Z]\.?|Jr\.?|Sr\.?|I{1,3}|IV", w)]
    if not (2 <= len(core) <= 4):
        return False
    # 縮寫與技術代號不是人名：CAR T、EGFR L858M、HER2 IHC 都長得像
    # 「兩個大寫開頭的詞」，但只要有一個詞是全大寫就不是人名。
    if any(w.isupper() for w in core):
        return False
    return all(w[:1].isupper() for w in core)


# 技術標籤是中文，原文多半是英文，字面比對一定落空（2026-09-30 實測：
# 一篇黑色素瘤研究的五個技術標籤全被丟掉，名額被 CDK4、MCL1 這類基因代號
# 佔滿，讀者看到一串代號不知所云）。所以中文技術詞另外認它的英文說法：
# 只要原文出現對應的英文，這個中文標籤就算有根據，仍然不是憑空生成。
_ZH_EN_TERMS = {
    "全基因體定序": ("whole-genome sequencing", "whole genome sequencing", "WGS"),
    "全外顯子定序": ("whole-exome sequencing", "whole exome sequencing", "WES"),
    "次世代定序": ("next-generation sequencing", "next generation sequencing", "NGS"),
    "單細胞定序": ("single-cell sequencing", "single cell RNA", "scRNA"),
    "空間轉錄組學": ("spatial transcriptom",),
    "轉錄組學": ("transcriptom",),
    "蛋白質體學": ("proteom",),
    "代謝體學": ("metabolom",),
    "表觀遺傳學": ("epigenetic", "epigenom"),
    "甲基化分析": ("methylation",),
    "長讀長定序": ("long-read", "long read"),
    "體細胞拷貝數變異": ("somatic copy number", "copy number alteration", "copy-number alteration", "SCNA", "CNV"),
    "拷貝數變異": ("copy number", "copy-number", "CNV"),
    "結構變異分析": ("structural variant", "structural variation"),
    "點突變分析": ("point mutation",),
    "腫瘤突變負荷": ("tumor mutational burden", "tumour mutational burden", "TMB"),
    "腫瘤異質性分析": ("heterogeneity",),
    "克隆重建": ("clonal reconstruction", "clonal evolution", "phylogen"),
    "液態生物檢體": ("liquid biopsy",),
    "循環腫瘤 DNA": ("circulating tumor DNA", "circulating tumour DNA", "ctDNA"),
    "微小殘留病灶": ("minimal residual disease", "molecular residual disease", "MRD"),
    "基因編輯": ("gene editing", "CRISPR"),
    "多基因風險評分": ("polygenic risk score", "polygenic score", "PRS"),
    "機器學習": ("machine learning", "deep learning", "neural network"),
    "人工智慧": ("artificial intelligence", " AI ", "AI-"),
    "生物資訊分析": ("bioinformatic",),
    "微生物體分析": ("microbiome", "microbiota"),
    "宏基因體定序": ("metagenom",),
    "數位 PCR": ("digital PCR", "dPCR", "ddPCR"),
    "即時定量 PCR": ("real-time PCR", "qPCR", "RT-PCR"),
    "流式細胞術": ("flow cytometry",),
    "質譜分析": ("mass spectrometry",),
    "免疫組織化學": ("immunohistochem", "IHC"),
    "伴隨式診斷": ("companion diagnostic",),
    "藥物基因體學": ("pharmacogenom",),
}


def _mentions_stem(stem: str, haystack: str) -> bool:
    """英文詞根比對：左邊要是詞界，右邊允許接下去（transcriptom 要能對上
    transcriptomic、transcriptomics）。上面對照表寫的是詞根不是完整字，
    所以不能用 _mentions 的雙邊詞界。"""
    if not stem or not haystack:
        return False
    # 連字號的寫法各家不同（copy number／copy-number），一律當空白比對。
    flat_stem = stem.replace("-", " ")
    flat_hay = haystack.replace("-", " ")
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(flat_stem)}", flat_hay, re.I) is not None


def _has_basis(tag: str, text: str) -> bool:
    """標籤在原文有沒有根據：字面出現，或中文技術詞的英文說法出現。"""
    if _mentions(tag, text):
        return True
    return any(_mentions_stem(en, text) for en in _ZH_EN_TERMS.get(tag, ()))


def _is_bare_symbol(tag: str) -> bool:
    """像 CDK4、CDKN2A、TP53BP1 這種裸基因／蛋白代號。

    這類代號對讀者幾乎沒有資訊量，一整排更是看不懂，所以留但要限量，
    讓廠商與技術標籤先佔位（2026-09-30 使用者回饋「CDK4MCL1CD276…
    這是什麼鬼」）。
    """
    return re.fullmatch(r"[A-Z][A-Z0-9]{1,8}(-[A-Z0-9]{1,4})?", tag) is not None


_MAX_BARE_SYMBOLS = 2


def _grounded(tags: list, text: str, media_names: set[str]) -> list[str]:
    """只留下在原文有根據、而且不是媒體名或人名的標籤。

    模型抽出來但原文找不到的一律丟掉（防幻覺）；出處與受訪者丟掉（不是
    讀者要抓的重點）；裸代號限量，不讓它排擠掉廠商與技術。
    """
    out = []
    symbols = 0
    for t in tags:
        if not isinstance(t, str) or not t.strip():
            continue
        tag = t.strip()
        if not _has_basis(tag, text):
            continue
        if tag.lower() in media_names:
            continue
        if _looks_like_person(tag):
            continue
        if _is_bare_symbol(tag):
            if symbols >= _MAX_BARE_SYMBOLS:
                continue
            symbols += 1
        out.append(tag)
    return out


def _media_names(conn: sqlite3.Connection) -> set[str]:
    """池裡實際用過的來源名稱，當成媒體名黑名單。"""
    rows = conn.execute("SELECT DISTINCT source_name FROM articles").fetchall()
    names = {(r["source_name"] or "").strip().lower() for r in rows}
    # 來源名稱常帶括號說明（「GeneOnline (台灣)」），括號前那段也要擋。
    for n in list(names):
        base = re.split(r"[（(]", n)[0].strip()
        if base:
            names.add(base)
    return {n for n in names if n}


def tags_for_article(row: sqlite3.Row, config: dict, media_names: set[str] | None = None) -> list[str]:
    """一篇文章的顯示標籤：廠商優先，再補技術與產品。全部經過原文驗證。"""
    text = f"{row['title'] or ''}\n{row['content'] or ''}"
    tags = vendor_tags(text, config)

    def _load(field: str) -> list:
        try:
            return json.loads(row[field]) or []
        except (TypeError, ValueError, IndexError, KeyError):
            return []

    # 產品名（發佈判定存的）比技術詞更具體，排在技術前面。
    try:
        product = row["release_product"]
    except (IndexError, KeyError):
        product = None
    if product:
        for p in re.split(r"[,、/]", product):
            p = p.strip()
            if p and _mentions(p, text) and p not in tags:
                tags.append(p)

    for field in ("tech_tags_json", "entity_tags_json"):
        for t in _grounded(_load(field), text, media_names or set()):
            if t not in tags:
                tags.append(t)

    return tags[: config["edm"]["max_tags"]]


def tags_for_topic(conn: sqlite3.Connection, topic_id: int, config: dict) -> list[str]:
    """一個話題的標籤：底下每篇（不含被擋掉的）各自取標籤後合併。"""
    rows = conn.execute(
        """SELECT title, content, tech_tags_json, entity_tags_json, release_product
           FROM articles WHERE topic_id = ? AND discarded_at IS NULL
             AND (gate_status IS NULL OR gate_status != 'excluded')
           ORDER BY published_at DESC""",
        (topic_id,),
    ).fetchall()
    media = _media_names(conn)
    merged: list[str] = []
    symbols = 0
    for row in rows:
        for t in tags_for_article(row, config, media):
            if t in merged:
                continue
            # 裸代號的限量要在合併後再算一次，不然一個話題下三篇文章
            # 各帶兩個代號就又變成一整排。
            if _is_bare_symbol(t):
                if symbols >= _MAX_BARE_SYMBOLS:
                    continue
                symbols += 1
            merged.append(t)
    return merged[: config["edm"]["max_tags"]]


def is_primary(conn: sqlite3.Connection, topic_id: int, config: dict) -> bool:
    """這則要放主要報導還是額外報導。

    兩個條件的聯集：來源在 NBDMD 清單裡，或文章主角是 watchlist 上的廠商。
    只看來源會漏掉「Illumina 的動態出現在 Fierce Biotech 上」這種讀者最
    關心的情況。
    """
    primary_ids = set(config["edm"]["primary_source_ids"])
    rows = conn.execute(
        """SELECT source_id, title, content FROM articles
           WHERE topic_id = ? AND discarded_at IS NULL
             AND (gate_status IS NULL OR gate_status != 'excluded')""",
        (topic_id,),
    ).fetchall()
    for row in rows:
        if row["source_id"] in primary_ids:
            return True
        if vendor_tags(f"{row['title'] or ''}\n{row['content'] or ''}", config):
            return True
    return False
