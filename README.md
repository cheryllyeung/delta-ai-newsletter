# Delta AI Newsletter

台達內部同仁的 AI 電子報自動化專案 — 定期彙整外部 AI/科技趨勢，透過 LLM 生成內容，並排版成有設計感的電子報發送。

技術細節（內容收錄標準、架構選型理由、pipeline 開發邏輯）見 [`docs/architecture.md`](docs/architecture.md)。

## 專案決策紀錄

| 項目 | 決定 |
|---|---|
| 內容來源 | 外部 AI / 科技新聞（公開來源），聚焦外部趨勢，不涉及內部機敏資料 |
| LLM 串接 | 外部 LLM API（Claude API） |
| 電子報形式 | 需要有設計感的排版（HTML template），非純文字 |
| 發送管道 | 待定（Email 為預設方向，可後續調整） |

## 內容管線

```
資料來源（arXiv / Reddit） → 收集/正規化 → 去重/依熱門度篩選 → LLM 依領域摘要與改寫
    → 人工編審 → HTML 模板渲染 → 發送 → 成效追蹤
```

## 內容分類策略：依台達事業群 x 領域

電子報內容不是單一大雜燴，而是依照台達的四大事業群拆成對應區塊，
讓各領域同仁優先看到跟自己專長相關的 AI 議題。完整設定在
[`config/domains.yaml`](config/domains.yaml)，之後新增/調整領域只需要改設定檔。

| 事業群 | 子領域 |
|---|---|
| 零組件 | 電源及系統、風扇與散熱管理 |
| 交通 | 電動車動力系統 |
| 自動化 | 工業自動化、樓宇自動化 |
| 基礎設施 | 資通訊基礎設施、電力暨能源解決方案、視訊與顯像系統 |

每個子領域各自對應：
- **arXiv 分類**：抓該領域相關的最新學術論文（技術面、偏前瞻）
- **Reddit 子版**：抓熱門討論，補足「實作經驗、踩雷心得」這類論文不會寫的內容
- 另外有一個跨領域的「AI 通識趨勢」區塊，放給所有讀者看的大方向新聞（新模型發布、產業動態等）

兩種來源型態互補：arXiv 偏「這個技術現在做到哪裡」，Reddit 偏「別人實際用起來踩了什麼坑」。
生成階段（`generation/summarize.py`）會依來源類型調整摘要重點，並要求 LLM 只根據提供的原文改寫、不得編造細節。

### 之後可以擴充的來源

目前只接了 arXiv 和 Reddit，之後若需要更即時或更垂直的內容，可以考慮：
Hacker News（綜合科技熱度）、特定廠商技術部落格（RSS）、YouTube 技術頻道字幕摘要等，
接法都是新增一個 `ingestion/xxx_source.py`，輸出統一的 `RawItem`，其他流程不用改。

## 目錄結構

- `ingestion/` — 各來源抓取模組（`arxiv_source.py`、`reddit_source.py`）與共用資料模型（`base.py`）
- `pipeline/` — 去重、依熱門度排序/篩選邏輯（`dedupe.py`）
- `generation/` — Claude API 串接，依領域摘要與改寫成電子報段落（`summarize.py`）
- `review/` — 人工審核流程（草稿輸出、修改、核准）— 待實作
- `render/` — 電子報 HTML 渲染邏輯（`build_newsletter.py`）
- `templates/` — 兩種視覺模板：
  - `newsletter.html.jinja` — email發送用，單欄、table-based、相容Outlook等信箱客戶端
  - `briefing.html.jinja` — 橫向情報看板，各事業群/子領域並排成欄，供內部網頁/連結檢視（非email格式，多欄排版在信箱客戶端相容性差）
- `distribution/` — 發送邏輯與收件名單管理 — 待實作
- `analytics/` — 開信率/點擊率等成效追蹤 — 待實作
- `config/` — 事業群/領域分類與資料來源設定（`domains.yaml`）
- `scripts/run_pipeline.py` — 串起自動化管線（arXiv + Reddit）的執行入口，產出email版
- `scripts/render_this_week_briefing.py` — 手動彙整當期橫向看板內容並渲染，見下方說明
- `docs/` — 專案文件

## 如何執行（MVP：產出草稿，不自動發送）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 ANTHROPIC_API_KEY，以及選填的 REDDIT_* 憑證
python -m scripts.run_pipeline
```

執行後會在 `output/drafts/<日期>.html` 產出當期電子報草稿，供人工檢視後再手動發送。
未設定 Reddit 憑證時該來源會自動略過，不影響 arXiv 部分正常運作。

### 已知限制

- Reddit 公開 JSON endpoint 近年對未授權請求限制嚴格，因此改用官方 OAuth（PRAW），
  需要免費申請一組 script 類型 App 憑證，見 `.env.example` 說明。
- arXiv API 官方限制為 3 秒一次請求，`ingestion/arxiv_source.py` 已內建節流，
  抓取多個領域時整體會需要一些時間，屬正常現象。

## 橫向情報看板（briefing）

`scripts/render_this_week_briefing.py` 目前是手動彙整版本：在 Reddit 憑證還沒設定前，
`run_pipeline.py` 自動化管線只能穩定抓到 arXiv 部分，缺少社群討論這塊內容。
這支腳本示範完整資料結構（每欄9個領域各5則，含研究/新聞/社群三種來源標籤），
內容為透過即時網路搜尋人工彙整、並附上可查證的原始連結。

```bash
python -m scripts.render_this_week_briefing
```

之後 Reddit 憑證補齊、且把 `run_pipeline.py` 的輸出接到 `render_briefing()`，
就能讓橫向看板也走自動化流程，取代這支手動腳本。

## 安全考量

- 僅將公開來源的新聞內容送入外部 LLM API，不傳遞任何台達內部機敏資訊。
- 收件名單與個資需符合公司資安規範，相關設定不進版控（見 `.gitignore`）。

## 路線圖

- **MVP**：手動觸發 → 固定幾個新聞來源 → LLM 生成草稿 → 人工微調 → 手動發送
- **V1**：排程自動化 + 簡易審核介面 + 自動發送
- **V2**：分眾內容、成效分析、回饋迴路優化選題
