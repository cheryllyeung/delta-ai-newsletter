# Delta AI Newsletter

幫台達同仁做 AI 趨勢情報的自動化系統。每天從 26 個公開來源抓 AI 相關內容，
把同一件事的多篇報導聚成「話題」，依台達 25 個以上單位對應的 18 個模組打分，
過門檻的寫成中文短文出日報，每週一挑最重要的 10 則出週報，文章裡的實體關係
同步累積成 Neo4j 知識圖譜。

這份 README 給工程師看：怎麼跑起來、程式擺哪、資料長什麼樣、改東西要注意
什麼。整條 pipeline 的邏輯脈絡在 **[`docs/PIPELINE.md`](docs/PIPELINE.md)**，
每個門檻值怎麼定的在 **[`docs/DECISIONS.md`](docs/DECISIONS.md)**。

## 快速開始

```bash
pip install -r requirements.txt          # 套件目前裝在全域 Python（待補 venv）
cp .env.example .env                     # 填 LLM_API_KEY、LLM_BASE_URL、NEWSLETTER_MODEL

python -m scripts.ingest_topics          # 建池：抓取 → 聚類 → 標籤 → 打分
python -m scripts.compose_topic_issue    # 出刊：選題 → 生成 → 自檢 → 存檔
python -m scripts.serve_topics           # 網頁：http://localhost:8001
```

常用參數：

```bash
python -m scripts.ingest_topics --concurrency 8 --build-graph   # 併發打標打分＋建圖
python -m scripts.ingest_topics --limit 8                        # 小樣本驗品質
python -m scripts.compose_topic_issue --date 2026-08-25 --cadence daily
python -m scripts.compose_topic_issue --date 2026-08-31 --cadence weekly
python -m scripts.serve_topics --host 0.0.0.0                    # 對內網開放（必須先設 NEWSLETTER_WEB_PASSWORD）
```

## 目錄結構

| 目錄 | 內容 |
|---|---|
| `ingestion/` | 各來源的抓取器（RSS、arXiv、HN、Reddit、GitHub、StackExchange）。共通資料契約是 `base.py` 的 `RawItem` |
| `pipeline/` | 核心邏輯：收錄關卡（gates）、聚類（topic_clustering）、標籤（article_tagging）、打分（module_scoring）、選題（topic_selection）、資料層（topic_db）、翻譯、專欄（delta_column）、圖譜（triple_extraction／entity_resolution／graph_store） |
| `generation/` | 文章生成（topic_generate） |
| `review/` | 出刊前自檢（topic_selfcheck） |
| `scripts/` | 入口：ingest_topics、compose_topic_issue、serve_topics、排程用的 run_daily.ps1／run_weekly.ps1／start_neo4j.ps1 |
| `prompts/` | 所有 LLM 指令（`<system>`／`<user>` 兩段式，prompt_loader 讀） |
| `config/topics.yaml` | 唯一的設定檔：來源清單、模組定義、門檻、配額 |
| `templates/` | 網頁模板（Jinja2） |
| `tools/` | 補資料與評測工具（backfill_*、eval_*、export_*、repair_*） |
| `tests/` | 冒煙測試與固定測資（`tests/data/clustering_pairs.yaml` 是聚類評測集） |
| `legacy/` | 已凍結的前兩條產品線，不再維護，import 路徑是舊的、跑不起來是刻意的 |

## 資料落點

| 位置 | 內容 | 進版控？ |
|---|---|---|
| `data/topics.db` | SQLite：articles／topics／issues／generated_topics／selection_trace | 否 |
| `data/qdrant` | 文章語意向量（Qdrant embedded 模式，不用起伺服器） | 否 |
| Neo4j（bolt://localhost:7687） | 知識圖譜。zip 版免安裝，`scripts/start_neo4j.ps1` 自動起 | 否 |
| `llm_logs/` | 每次 LLM 呼叫的完整輸入輸出，除錯與回溯用 | 否 |
| `runs/` | 排程執行紀錄（runs/daily/、runs/weekly/） | 否 |

資料表的欄位語意都寫在 `pipeline/topic_db.py` 的 `_SCHEMA` 註解裡。
migration 走「ALTER TABLE 加欄位、失敗就當已存在」的模式，見
`get_connection()`。

## 環境變數（.env）

必填：`LLM_API_KEY`、`LLM_BASE_URL`（公司內網 gateway）、`NEWSLETTER_MODEL`。
選填：`NEWSLETTER_WRITING_MODEL`（寫作／翻譯單獨用一顆）、
`NEWSLETTER_REVIEW_MODEL`（自檢換一顆審查模型）、`NEO4J_*`（沒設就跳過建圖）、
`NEWSLETTER_WEB_USER`／`NEWSLETTER_WEB_PASSWORD`（網頁 Basic Auth，
對內網開放時必填，沒設會拒絕啟動）、`REDDIT_*`。完整說明見 `.env.example`。

## 排程（Windows 工作排程器）

| 工作 | 時間 | 做什麼 |
|---|---|---|
| DeltaAI-DailyIssue | 每天 12:00 | `run_daily.ps1`：起 Neo4j → ingest（含建圖）→ 出前一天的日報 |
| DeltaAI-WeeklyIssue | 每週一 12:10 | `run_weekly.ps1`：出上週日到週六的週報（含台達專欄與大標題） |

兩個都設了錯過補跑。**注意：.ps1 檔必須存成 UTF-8 with BOM**，無 BOM 的
中文註解會讓 Windows PowerShell 5.1 解析失敗、排程整個不跑（發生過，
排程連續失敗十幾天沒人發現）。

## 改東西之前要知道的

1. **冪等是底線**。每一步只處理「還沒做過那一步」的資料，任何一步中斷，
   重跑會自動接續。新增步驟時維持這個性質
2. **出刊有防重複**：同一天同頻率已有期數就跳過，補跑不會出兩份
3. **LLM 呼叫一律走 `pipeline/llm_client.py`** 的 `create_chat_completion`
   （帶重試）＋ `llm_logging.log_call`（留檔）。不要自己 new client
4. **prompt 改了要想清楚影響範圍**：打分 prompt 改了，新舊分數不可比，
   通常要全池重打（把 `module_scores_json` 清成 NULL，ingest 會自動撿）
5. **聚類、實體解析都是「三段式灰色地帶」模式**：便宜計算先分流、模型只
   判中間地帶。要調門檻先跑 `tools/eval_clustering_pairs.py` 看 before/after
6. **已出刊的期數不改**。修正錯誤的做法是修規則＋全池回溯重建，不是改
   單篇歷史
7. 門檻值不要憑感覺調。每個值在 `docs/DECISIONS.md` 都有依據跟驗證狀態，
   調整前先量、調整後把帳補上

## 測試與評測

```bash
python -m tests.smoke_test_topic_pipeline      # 全流程冒煙（會打 LLM）
python -m tests.smoke_test_entity_resolution   # 實體解析回歸
python -m tools.eval_clustering_pairs          # 聚類 19 組固定測資
python -m tools.eval_scoring_variance          # 打分重複性
python -m tools.repair_false_merges --dry-run  # 掃假合併（只印不動資料）
```

## 文件地圖

| 文件 | 給誰 | 內容 |
|---|---|---|
| `docs/PIPELINE.md` | 主管、新進者 | 整條邏輯脈絡，白話＋技術 |
| `docs/DECISIONS.md` | 工程師 | 每個門檻值的依據與驗證狀態 |
| `docs/status/` | 追進度的人 | 各日期的狀態報告與 QA |
| `README.md`（本檔） | 工程師 | 怎麼跑、擺哪、注意什麼 |
