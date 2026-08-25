"""收錄判定：一則內容從進池到上刊要過的三道關，判斷邏輯全部集中在這一支。

## 為什麼要有這支

在這支之前，整條流程沒有任何一步會說「這篇不收」。抓進來的全留、標籤全留、
打分全留，最後靠 config/topics.yaml 的 selection.total_topics 版位上限把其餘
的擠掉。結果是「為什麼這篇沒上」只能回答「版位滿了」，沒辦法回答「它不夠格」。
這兩件事對讀者跟對審稿的人是完全不同的意思，混在一起講就沒有說服力。

所以把判定拆成三道，每一道都有明確的理由碼，理由碼跟觸發它的數值一起寫進
資料庫（articles.gate_reason 與 selection_trace 表），網頁上直接看得到。

| 關卡 | 層級 | 在哪裡呼叫 | 處理的事 |
|---|---|---|---|
| Gate 1 | 文章 | scripts/ingest_topics.py | 太短的降級成訊號、太舊的與非 AI 的排除 |
| Gate 2 | 話題 | pipeline/topic_selection.py | 沒有可寫內容、還沒打分的話題不進候選 |
| Gate 3 | 出刊 | pipeline/topic_selection.py + scripts/compose_topic_issue.py | 分數不夠、素材不足、自檢沒過、版位滿 |

## 三種狀態，不是兩種

文章判定的結果有三種，這是這支最重要的設計：

| 狀態 | 意思 | 會發生什麼 |
|---|---|---|
| `included` | 正常收錄 | 聚類、標籤、打分、可當寫作素材 |
| `signal_only` | 只有標題沒有內文，當熱度訊號用 | 聚類（算「幾家在報導」），但不標籤、不打分、不當寫作素材 |
| `excluded` | 不該收 | 什麼都不做 |

`signal_only` 這一層是必要的：池裡 43% 的文章是只有標題的 RSS 殘缺內容，
而且集中在 Hacker News、GitHub、DIGITIMES 這幾個來源（各自 100%、89%、100%）。
這些來源的價值本來就是「有人在討論這件事」的訊號，不是內文。一刀切成排除會
整批失去這個訊號，一刀切成收錄則會讓模型拿著一行標題硬寫一篇文章，兩種都錯。

## 拒絕不等於刪除

被擋掉的文章一律留在 articles 表裡，只是標上 gate_status，不會被清掉。理由跟
這個專案一開始就決定的「不做早期硬篩選」一致：資料留著才能回頭算「這批來源
的淘汰率多少」「這條門檻調鬆一點會多收幾篇」，刪掉就只剩下印象。

實際的節省在於被擋掉的文章不會進標籤、打分，省下來的是 LLM 額度不是硬碟空間。

## 門檻值放哪裡

數值全部在 config/topics.yaml 的 gates 區塊，這支只有判斷邏輯。每個門檻怎麼
定的、依據什麼數據，寫在 docs/DECISIONS.md，不在這裡重複。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

INCLUDED = "included"
SIGNAL_ONLY = "signal_only"
EXCLUDED = "excluded"


@dataclass(frozen=True)
class GateResult:
    """一次判定的結果。

    status 不是 included 時 reason 一定有值，detail 放觸發判定的實際數值
    （例如「內文只有 187 字，門檻 200」），因為理由碼本身只說「太短」，
    說不出「差多少」，而審稿的人真正想知道的是後者。
    """

    status: str
    reason: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == INCLUDED

    @classmethod
    def ok(cls) -> "GateResult":
        return cls(status=INCLUDED)

    @classmethod
    def signal_only(cls, reason: str, **detail: object) -> "GateResult":
        return cls(status=SIGNAL_ONLY, reason=reason, detail=dict(detail))

    @classmethod
    def reject(cls, reason: str, **detail: object) -> "GateResult":
        return cls(status=EXCLUDED, reason=reason, detail=dict(detail))


# 理由碼 → 給人看的說明。網頁跟報告都讀這一份，不要在樣板裡另外寫一次中文，
# 不然改了理由碼會有一邊忘記跟著改。
REASON_LABELS: dict[str, str] = {
    # Gate 1：文章層
    "title_only": "只抓得到標題，沒有內文可以寫，只能當『有人在討論』的訊號",
    "published_out_of_window": "發布時間太舊，跟不上這期的時效",
    "not_ai_related": "讀完發現內容其實跟 AI 沒什麼關係",
    # Gate 2：話題層
    "no_substantive_article": "底下沒有一篇內容夠完整的文章，硬寫只會湊字數",
    "not_scored": "還沒被 18 個業務面向評分完，這次還排不上候選",
    # Gate 3：選題
    "below_module_threshold": "每個業務面向看過都覺得不夠切題，沒有一項過關",
    "content_type_quota_full": "這一期同類型的內容已經選夠了，是被同類型排名更前面的話題卡掉的，不是這篇不好",
    "module_group_quota_full": "這一類業務（本業或職能）這期的名額已經用完",
    "tier_quota_full": "這種來源等級這期的名額已經用完",
    "slots_full": "這期版面就這麼多，排序沒排到會上刊的名次",
    # Gate 3：生成
    "insufficient_sources": "找不到夠相關的素材佐證，硬寫出來會太單薄",
    "failed_selfcheck": "重寫過一次還是沒有把握，寧可不上也不濫竽充數",
    "generation_error": "生成過程出了技術問題，跟內容好壞無關",
}

# 「不夠格」跟「版位滿」要分開統計。前者代表這批候選的品質問題，後者代表
# 版面規模的取捨，兩種訊號的處理方式完全不同：前者要回頭看來源跟門檻，
# 後者只要調 total_topics。網頁上的選題帳也照這個分類分兩欄顯示。
QUOTA_REASONS = frozenset(
    {"content_type_quota_full", "module_group_quota_full", "tier_quota_full", "slots_full"}
)


def is_quota_reason(reason: str | None) -> bool:
    return reason in QUOTA_REASONS


def label_for(reason: str | None) -> str:
    if reason is None:
        return "入選"
    return REASON_LABELS.get(reason, reason)


def _status_of(row) -> str:
    """讀一列 articles 的 gate_status，舊資料是 NULL 時當作 included。

    不追溯處罰已經在池裡的東西：一次改設定就讓歷史候選池整批消失，比沒有
    判定還糟。要讓舊資料也走一次判定請跑 tools/backfill_article_gates.py。
    """
    try:
        return row["gate_status"] or INCLUDED
    except (IndexError, KeyError):
        return INCLUDED


def substantive(article_rows: list) -> list:
    """挑出可以當寫作素材的文章（狀態是 included 的）。"""
    return [r for r in article_rows if _status_of(r) == INCLUDED]


def signal_articles(article_rows: list) -> list:
    """挑出只能當熱度訊號的文章。算「幾家在報導」時要含進來。"""
    return [r for r in article_rows if _status_of(r) == SIGNAL_ONLY]


# ---------------------------------------------------------------- Gate 1：文章


def check_article_intake(
    *,
    content: str,
    published_at: datetime,
    config: dict,
    now: datetime | None = None,
) -> GateResult:
    """Gate 1a：入池當下就能判斷的確定性檢查，不花 LLM 額度。

    刻意排在標籤之前：標籤每篇要打一次 gateway，先把只有標題的東西降級掉，
    省下來的是每天實際要等的時間（池裡有 43% 是這種）。

    注意這裡不做網址去重，那件事由 topic_db.insert_article_if_new() 的
    UNIQUE 約束處理，已經是資料庫層級的保證，不需要在應用層再判一次。

    日期先判、長度後判：一篇既太舊又只有標題的文章，「太舊」是更根本的
    理由，記成 excluded 比記成 signal_only 更符合實際處置。
    """
    gate_cfg = config["gates"]["article"]
    now = now or datetime.now(timezone.utc)

    published = published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_days = (now - published).total_seconds() / 86400
    max_age = gate_cfg["max_age_days"]
    if age_days > max_age:
        return GateResult.reject(
            "published_out_of_window", age_days=round(age_days, 1), threshold=max_age
        )

    text = (content or "").strip()
    min_chars = gate_cfg["min_content_chars"]
    if len(text) < min_chars:
        return GateResult.signal_only("title_only", chars=len(text), threshold=min_chars)

    return GateResult.ok()


def check_article_tagged(tags: dict, config: dict) -> GateResult:
    """Gate 1b：標籤跑完之後才判斷得出來的部分。

    目前只有一條：這篇到底跟 AI 有沒有關係。看起來理所當然，但在這條加進來
    之前完全沒有人在管。來源清單裡 TechCrunch、Hacker News、Slashdot、
    DIGITIMES 這類綜合性來源本來就會夾帶跟 AI 無關的文章，全部照收進候選池，
    再靠 18 模組打分去「稀釋」它們。但打分那一步的提示詞問的是「跟這個模組
    多相關」，不是「這篇算不算 AI 內容」，所以一篇講募資或講電網的純產業
    新聞也可能拿到不低的策略投資或能源電力分數，然後就上刊了。

    is_ai_related 由 prompts/article_tagging.md 產出。舊資料沒有這個欄位
    （2026-08-14 之前標的），tags 裡讀不到時當作通過。
    """
    if not config["gates"]["article"]["require_ai_related"]:
        return GateResult.ok()

    if tags.get("is_ai_related") is False:
        return GateResult.reject("not_ai_related", note=tags.get("ai_relevance_reason") or "")
    return GateResult.ok()


# ---------------------------------------------------------------- Gate 2：話題


def check_topic_ready(*, article_rows: list, module_scores: dict | None, config: dict) -> GateResult:
    """Gate 2：這個話題有沒有資格進選題候選池。

    「單篇也算話題」是刻意允許的（gates.topic.allow_single_source）。目前
    來源以企業案例與科技媒體為主，同一件事被多家報導、需要合併成一個話題的
    情況本來就少，191 個話題裡有 184 個底下只有一篇文章。如果規定「至少兩篇
    才算話題」，整個候選池會少掉 96%，那不是嚴謹是自廢武功。

    但單篇話題會被標記出來（見 is_single_source()），在選題帳跟文章頁面上
    如實揭露「這則只有一個來源」，讓讀者自己判斷份量。要求多來源才算話題
    這件事，等接進更多一般新聞來源、聚類真的開始合併之後再談。

    真正擋的是另一件事：話題底下一篇「內文夠長」的文章都沒有。那種話題就算
    熱度再高也寫不出東西，模型只能從一行標題瞎掰。
    """
    topic_cfg = config["gates"]["topic"]

    usable = substantive(article_rows)
    min_substantive = topic_cfg["min_substantive_articles"]
    if len(usable) < min_substantive:
        return GateResult.reject(
            "no_substantive_article",
            substantive_count=len(usable),
            signal_count=len(signal_articles(article_rows)),
            threshold=min_substantive,
        )

    if not topic_cfg["allow_single_source"] and is_single_source(article_rows):
        return GateResult.reject("single_source_not_allowed", source_count=1)

    if not module_scores:
        return GateResult.reject("not_scored")

    return GateResult.ok()


def is_single_source(article_rows: list) -> bool:
    """這個話題是不是只有一家在講。

    看的是不重複來源數，不是文章數：同一個部落格連發兩篇講同一件事，聚類會
    併成一個話題，但那仍然只是一方說法，不該算成「多家報導」。

    算的時候把 signal_only 的文章也含進來，因為那正是這個狀態存在的意義：
    Hacker News 上有人貼了同一件事，即使它只有一行標題，也確實構成「另一家
    在講」的證據。
    """
    counted = substantive(article_rows) + signal_articles(article_rows)
    return len({r["source_id"] for r in counted}) <= 1
