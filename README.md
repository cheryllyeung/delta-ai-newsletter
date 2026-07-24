# Delta AI Newsletter

一套幫台達同仁做「AI 趨勢情報彙整」的自動化系統。核心工作是每週從外部公開來源（學術論文、技術社群、產業新聞）大量抓取跟 AI 有關的內容，透過相關性計分和去重演算法把雜訊濾掉，再用 LLM 依台達四大事業群的實際業務脈絡重新整理、翻譯、寫出「這跟我們的產品線有什麼關係」，最後排版成電子報或情報看板送到對的人手上。

重點在準，不在多。與其讓工程師每週自己滑十幾個網站找相關資訊，不如讓系統先把噪音濾掉，只留下真正跟台達業務相關、有價值判斷的內容。

## 這套系統怎麼運作

整體流程分成五個階段：

```
抓取（多來源爬蟲） → 計分與去重 → LLM 依領域重寫 → 人工審核 → 排版發送
```

**第一階段：抓取。** 系統同時對接多個資料來源，各自負責不同性質的內容：

- **arXiv**：抓各領域對應的學術分類（如電源系統對應 `eess.SY`），拿到最新的技術論文，代表「這個技術現在做到哪一步」
- **Reddit**：抓對應子版的熱門討論（用官方 OAuth API，過濾掉置頂貼、只留 upvote 數夠高的），代表「別人實際用起來踩了什麼坑」，這是論文裡不會寫的東西
- 兩者互補，一個講理論進展，一個講實戰經驗

**第二階段：計分與去重，這是精準度的核心。** 抓下來的每一筆資料都會過一層加權算分：以 arXiv 為例，論文先靠 `arxiv_categories`（分類代碼）做第一層相關性篩選，再用一組人工整理的關鍵字對標題和摘要做命中計分，命中越多分數越高。零命中也不會被硬性丟棄，因為分類本身已經是主要判準，關鍵字只是拿來做排序的加權。之後同網址的重複項目只留分數較高的那一筆，每個子領域最後只取分數前幾名，濾掉長尾雜訊。

**第三階段：LLM 重寫。** 篩選過的原始資料丟給 Claude，依照每個子領域的業務描述重新整理：翻譯成中文、抓重點、寫一句「這跟你的工作有什麼關係」。這裡刻意要求模型只能根據提供的原文改寫，不能自己編細節或編數據，避免生成內容失真。

**第四階段：人工審核。** 生成的草稿在正式發出前，需要有人看過確認內容判斷沒問題。這一關目前還在補齊中，細節見下方「還在做的部分」。

**第五階段：排版發送。** 同一份內容資料可以套用兩種版型：一份是給信箱看的電子報（單欄、相容 Outlook），一份是給網頁看的情報看板（依事業群分段、子領域並排成卡片，上下捲動瀏覽，適合快速掃描整週重點）。

## 為什麼要照事業群拆分

台達橫跨好幾個事業群，做電源的同仁不需要在意樓宇自動化的新聞。所以整份情報依照台達四大事業群、細分成八個子領域來源獨立抓取、獨立計分：

| 事業群 | 子領域 |
|---|---|
| 零組件 | 電源及系統、風扇與散熱管理 |
| 交通 | 電動車動力系統 |
| 自動化 | 工業自動化、樓宇自動化 |
| 基礎設施 | 資通訊基礎設施、電力暨能源解決方案、視訊與顯像系統 |

另外有一個跨領域的「AI 通識趨勢」欄位，放大方向新聞（新模型發布、產業動態），讓所有讀者都能掃過一輪業界在發生什麼事。

所有分類設定都集中在 [`config/domains.yaml`](config/domains.yaml)，要新增領域、調整關鍵字、換抓取來源，改這一份設定檔就好，不用碰程式碼。這樣未來如果台達組織調整或多了新的產品線，情報系統可以直接跟著改設定，不必重寫程式。

## 技術架構

這是一套純 Python 的批次處理系統，沒有常駐服務，也沒有資料庫。每次執行就是「跑一次腳本、產出一份檔案」：

```
config/domains.yaml（設定：抓什麼、怎麼算分）
        ↓
ingestion/（爬蟲：arXiv API、Reddit OAuth）
        ↓
pipeline/（去重、依分數排序取前 N 名）
        ↓
generation/（Claude API 依領域重寫、翻譯、給出關聯性判斷）
        ↓
render/（Jinja2 樣板渲染成 HTML）
        ↓
output/drafts/（靜態檔案，等人工審核後發送）
```

選這套架構的原因很直接：

- **Python**：LLM SDK、爬蟲套件、樣板引擎都成熟穩定，團隊接手維運不需要再學一套前端框架
- **設定檔驅動分類邏輯**：`domains.yaml` 把「抓什麼」「怎麼判斷相關」跟程式碼分開，改邏輯不用動 code，也讓非工程背景的人也能參與調整
- **靜態 HTML 而非網頁應用**：電子報本質是一份文件，讀者不需要跟頁面互動，用 Jinja2 產出靜態頁面就夠了，沒必要上 React 這類前端框架增加維護成本
- **目前沒有資料庫**：整套流程無狀態、每次全量抓取，還沒有「訂閱名單」「歷史查詢」這類需要長期保存資料的需求，先不引入資料庫維運成本；等未來要做開信率追蹤、分眾發送，再視需求疊加 SQLite 之類的輕量方案

新增資料來源的方式也是設計好的：只要照 `ingestion/base.py` 的 `RawItem` 格式輸出，後面的去重、計分、生成流程完全不用改。目前只接了 arXiv 跟 Reddit，之後如果要接 Hacker News、廠商技術部落格 RSS，或是 News API 補新聞類內容，都是新增一支 `ingestion/xxx_source.py` 就好。

## 目錄結構

- `ingestion/` — 各來源爬蟲：`arxiv_source.py`、`reddit_source.py`（趨勢彙整）、`case_source.py`（Delta Pulse 案例來源RSS）與共用資料格式（`base.py`）
- `pipeline/` — 去重排序（`dedupe.py`）；Delta Pulse 的評分（`case_scoring.py`）、文章池 schema/CRUD（`pool_db.py`）、標籤同義詞分群（`tag_clustering.py`，本機 embedding，不用API key）、趨勢計算（`trend.py`）、選文查詢（`pool_selection.py`）、prompt模板讀取（`prompt_loader.py`）、LLM client（`llm_client.py`）、呼叫落地記錄（`llm_logging.py`）
- `generation/` — Claude API 串接：`summarize.py`（趨勢彙整重寫）、`case_generate.py`／`case_intro.py`（Delta Pulse 案例生成／動態導言）
- `review/` — `case_selfcheck.py`：Delta Pulse 的獨立事實查核/風格審查關卡；`pending/` 是自動產生的待人工確認案例清單（不進版控）；舊 pipeline 的人工審核關卡仍待實作
- `render/` — `build_newsletter.py`：趨勢彙整版 HTML 渲染（Delta Pulse 改用網頁輸出，見 `scripts/serve_pulse.py`）
- `prompts/` — Delta Pulse 的 4 支 prompt 模板（`scoring.md`／`generate.md`／`intro.md`／`self_check.md`）與共用風格規範（`style_guide.md`），跟程式碼分離
- `templates/` — `newsletter.html.jinja`（電子報，單欄相容Outlook）、`briefing.html.jinja`（情報看板）、`pulse.html.jinja`＋`pulse_list.html.jinja`（Delta Pulse 網頁：單期詳細頁／期數列表）
- `distribution/` — 發送與收件名單管理，待實作
- `analytics/` — 開信率/點擊率追蹤，待實作
- `data/` — Delta Pulse 的 SQLite 文章池（`pulse.db`），不進版控
- `config/domains.yaml` — 事業群/領域分類與資料來源設定（趨勢彙整）
- `config/pulse.yaml` — 選文配額、評分權重、趨勢參數、案例來源清單（Delta Pulse）
- `scripts/run_pipeline.py` — 趨勢彙整管線入口，產出電子報版
- `scripts/render_this_week_briefing.py` — 產出當期情報看板
- `scripts/ingest_pool.py` — Delta Pulse：抓取+評分寫入文章池（可重複執行）
- `scripts/compose_issue.py` — Delta Pulse：對文章池組成一期
- `scripts/serve_pulse.py` — Delta Pulse：本機網頁（FastAPI）
- `scripts/smoke_test_case_pipeline.py` — Delta Pulse 離線邏輯測試（真實RSS + 假LLM回應）
- `docs/architecture.md` — 更細的規格文件（收錄標準、每個技術決策的完整理由）

## 怎麼跑起來

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 LLM_API_KEY,以及選填的 REDDIT_* 憑證
python -m scripts.run_pipeline
```

跑完會在 `output/drafts/<日期>.html` 產出當期草稿，人工看過確認沒問題再發送。沒設定 Reddit 憑證時，那個來源會自動跳過，不影響 arXiv 那半邊正常運作。

## 目前的真實進度

系統的骨架跟核心演算法都在了，但誠實說，還有幾塊沒補齊，這裡直接列出來：

- **Reddit 這個來源目前還沒真的接上**，程式碼寫好了但需要申請一組 OAuth 憑證才能啟用，詳見 `.env.example`
- **通用 AI 趨勢欄位缺一個新聞類來源**，arXiv 只有學術論文，沒有「新模型發布」這種新聞，所以目前這塊內容是人工用即時搜尋彙整，還沒自動化
- **人工審核關卡還沒做出介面**，目前是靠人工看 HTML 草稿把關，設計上應該要有一個「生成草稿 → 可編輯格式 → 核准 → 才能發送」的關卡，這是下一步要優先補的
- **情報看板（briefing）目前是手動彙整版本**，`scripts/render_this_week_briefing.py` 示範完整資料結構跟排版效果，等 Reddit 憑證跟新聞來源都補齊，就能讓看板也走自動化，取代這支手動腳本
- **arXiv API 有官方限制**，3 秒才能打一次，抓多個領域時整體會需要一點時間，這是正常現象，已經內建節流處理

這些缺口的優先順序、更完整的技術理由跟收錄標準，寫在 [`docs/architecture.md`](docs/architecture.md)。

## 第二條 pipeline：Delta Pulse 台達脈動（案例式週報，pool 化架構 v2）

上面整套是「AI 趨勢彙整」，讀者看的是論文與社群討論在講什麼。但同仁常常更想知道的是另一件事：別的公司實際怎麼把 AI 用進自己的工作流程，用了之後省下多少時間或錢。這是不同的內容邏輯，所以獨立做成第二條 pipeline，跟上面那條完全不共用設定檔跟樣板，只共用 `RawItem` 格式跟去重這幾個共用元件。

**v2 是一次架構重寫**（2026-07-23 開會後調整）：拿掉了「每週固定主題、抓了就篩、篩掉就丟」的線性做法，改成長期文章池：

- **不做早期硬篩選**：所有抓到的文章都存進 SQLite（`data/pulse.db`），都打分數，不會因為評分不理想就被刪除。`is_ai_application=false` 只會讓分數打折，文章仍然留在池裡
- **開放式標籤 + 趨勢偵測**：每篇文章由 LLM 自由貼 2-5 個 hashtag，統計整個池裡標籤出現頻率，近期密集出現的標籤代表「這正在變成趨勢」，該期選文時把相關文章的權重往上調
- **三分類配額**：分類軸是廠區現場／後勤支援／業務前台（取代原本 corporate/factory 二分法），配額上後勤支援、業務前台為主力、廠區現場占比低，因為廠區的人不太看信箱、推播不易觸及
- **沒有固定主題**：刊頭導言依「這期實際選出的文章組合」動態生成，不套用預先寫好的主題文案
- **輸出改成網頁**：本機跑一個輕量 FastAPI + SQLite 的網頁（POC 階段，部署到公司 VM 留到之後）

```
scripts/ingest_pool.py（可重複執行）
    抓 5 個真實 RSS 來源 → 依網址跟 DB 既有記錄去重，只處理沒看過的文章
    → Prompt 1 評分＋開放hashtag＋三分類（不淘汰）→ 全部寫進 SQLite

scripts/compose_issue.py（要出刊時執行）
    → 從 pool 算趨勢分數（近期tag頻率 vs 基期頻率）
    → 「LLM評分 × 趨勢權重」排序 + 三分類配額選文
    → Prompt 2 生成 + Prompt 4 獨立自檢（不過門檻回灌重新生成，最多2次）
    → 依實際選出的內容跑 Prompt 3 生成動態導言
    → 寫進 issues / generated_cases 表，標記文章已用過（不會被下一期重選）

scripts/serve_pulse.py（本機網頁）
    → FastAPI + Jinja2 讀 SQLite，期數列表 + 單期詳細頁
```

4 支 prompt 存在 `prompts/*.md`，跟程式碼分離；每次 LLM 呼叫的輸入輸出都會落地存到 `runs/{日期}/{階段}/`；自檢沒過門檻（confidence < 0.8）的案例會標成「待人工確認」。

**跑法：**

```bash
python -m scripts.smoke_test_case_pipeline   # 離線邏輯測試：真實RSS + 假LLM回應，不用API key、不花額度
python -m scripts.ingest_pool                # 抓取+評分，寫進長期文章池（需要 .env 設定 LLM_API_KEY）
python -m scripts.compose_issue              # 對文章池組成一期
python -m scripts.serve_pulse                # 啟動本機網頁，開瀏覽器連 http://localhost:8000
```

**目前的真實進度跟已知限制（誠實列出）：**

- 已驗證 5 個真實來源都能正常抓到完整內文（不是只有摘要）
- **factory（現改名廠區現場）類案例天生稀少**：這 5 個來源都是雲端/企業 IT 導向的 blog，選文邏輯會優雅降級（從其他分類遞補），不會為了湊配額硬選低分文章
- **去重目前只做網址正規化比對**，沒有做跨來源的語意相似度去重
- **趨勢偵測公式是簡單的近期/基期密度比**，POC 階段刻意不做複雜的時間序列模型，之後要調準度優先從 `pipeline/trend.py` 下手
- **尚未實際打過 Claude API 跑出真實案例草稿**：本地環境沒有可用的 `LLM_API_KEY`（卡在公司內部 LLM gateway 的連線問題，見 `docs/status_report_2026-07-23.md`），`scripts/smoke_test_case_pipeline.py` 已經驗證過整條邏輯（DB寫入、趨勢計算、配額選文、自檢重試、網頁渲染）不會壞，但真正的評分/生成/自檢文字品質，需要金鑰打通後才能看到
- **網頁只做到本機能看**，沒有部署、登入、權限這些，公司 VM 的串接留到 POC 驗證完之後

## 安全考量

- 只把公開來源的新聞內容送進外部 LLM API，不傳遞任何台達內部機敏資訊
- 收件名單跟個資需符合公司資安規範，相關設定不進版控（見 `.gitignore`）

## 路線圖

- **MVP（現在）**：手動觸發 → 固定幾個來源 → LLM 生成草稿 → 人工微調 → 手動發送
- **V1**：排程自動化 + 補齊新聞來源 + 簡易審核介面 + 自動發送
- **V2**：分眾內容、開信/點擊成效分析、依回饋優化選題演算法
