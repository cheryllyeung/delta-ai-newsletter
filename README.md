# Delta AI Newsletter

幫台達同仁做 AI 趨勢情報彙整的自動化系統。每天從公開來源抓 AI 相關內容，
把同一件事的多篇報導聚成「話題」，依台達 25+ 個單位對應的 18 個模組打分，
選出真的跟業務有關的幾則，寫成中文短文出刊。

重點在準，不在多。

## 三十秒看懂流程

```
建池（每天排程）   抓 18 個來源 → 收錄判定 → 聚成話題 → 貼標籤 → 18 模組打分
出刊（要出刊時）   篩當天候選 → 選題 → 取素材 → 寫作＋自檢 → 存檔
看                 本機網頁：期數列表、單期內容、選題帳
```

```bash
python -m scripts.ingest_topics          # 建池
python -m scripts.compose_topic_issue    # 出刊
python -m scripts.serve_topics           # 看：http://localhost:8001
```

完整的階段契約、每一步吃什麼吐什麼、失敗會怎樣，寫在
**[`docs/PIPELINE.md`](docs/PIPELINE.md)**。

每個門檻值怎麼定的、依據什麼資料、哪些還沒驗證，寫在
**[`docs/DECISIONS.md`](docs/DECISIONS.md)**。

## 核心概念

**話題，不是文章。** 打分、選題、寫作的單位都是話題不是單篇文章。多篇講
同一件事的報導會先用語意相似度聚成一個話題。目前來源特性使然，多數話題底下
只有一篇文章，這在 DECISIONS 裡有誠實交代。

**18 個模組，不是部門。** 台達橫跨零組件、能源、自動化、資通訊等事業群，
加上法務財會人資等後勤職能，超過 25 個單位。每個話題被這 18 個角度各打一次
分，選題時依配額輪流挑，避免整期偏向同一類。

| 群組 | 模組 |
|---|---|
| 職能（10） | 法務合規、財會稽核、人資、行銷品牌、資訊資安、營運物流、策略投資、研發工程、知識管理、EHS |
| 領域（8） | 能源電力、樓宇自動化、電動車車用、網通基礎設施、製造廠務、消費性產品、軟體平台、永續節能 |

**收錄有明確的關卡，落選有明確的理由。** 三道 gate 判定文章能不能用、話題
有沒有東西可寫、這篇能不能出刊。每個候選的去向都寫進資料庫，網頁的
`/issues/<id>/trace` 直接看得到「這一期從幾個候選裡選了幾則、其餘各是被哪
一關擋掉的」。「不夠格」跟「版位滿」分開統計，因為處理方式完全相反。

## 目錄

```
config/topics.yaml   唯一設定檔：來源、模組、配額、所有門檻值
prompts/             8 支 prompt，跟程式碼分離
ingestion/           抓取：6 種來源，共同輸出 RawItem
pipeline/            聚類、標籤、打分、收錄判定、選題、取素材、翻譯、圖譜
generation/          寫作
review/              自檢
scripts/             3 個入口 + run_daily.ps1（排程）
tools/               一次性補資料與匯出
tests/               離線 smoke test
docs/                PIPELINE.md、DECISIONS.md、prd/、status/
legacy/              已凍結的前兩條 pipeline，見 legacy/README.md
```

`pipeline/` 每支檔案對應哪一階段，見 [`docs/PIPELINE.md`](docs/PIPELINE.md)
最後那張表。

## 環境

```bash
pip install -r requirements.txt
cp .env.example .env    # 填 LLM_API_KEY / LLM_BASE_URL，其餘選填
python -m tests.smoke_test_topic_pipeline   # 離線驗證，不打 LLM
```

LLM 全部走台達內網的 gateway，資料不離開公司。embedding、聚類、reranker
跑在本機，不需要 API key。

知識圖譜（Neo4j）是選配：`NEO4J_URI` 沒設或連不上會自動跳過，不影響出刊。
建圖預設不做，要補圖用 `python -m tools.backfill_graph_extraction`。

## 目前的真實進度

**跑得起來的**：整條流程從抓取到出刊可以全自動跑完，每天清晨排程觸發。
已產出 9 期日報。embedding、Qdrant 聚類、reranker、實體解析都在本機驗證過。

**已知的洞**（優先順序與細節見 [DECISIONS](docs/DECISIONS.md) 最後一節）：

| # | 洞 |
|---|---|
| 1 | 人工審核只有一個布林旗標，沒有核准／退回流程 |
| 2 | 沒有評測集，改參數沒有 before/after 指標（打分 rubric 已加，但證明不了有沒有變好） |
| 3 | `is_ai_related` 判定的欄位 8/14 才修好，全池要重跑標籤才生效，誤判率未量 |
| 4 | 自檢是同一個模型查自己 |
| 5 | 聚類門檻（0.72／title-only 0.80）沒做過正式評測（要先有多家報導的題材才驗得起來） |

**幾個來源只有標題沒有內文**：DIGITIMES、Hacker News、InsideEVs 這三個來源
的 RSS 只給標題或圖說，我們沒有、也沒有試圖取得付費牆後的內容。這些條目
現在標成「只當熱度訊號」，會算進「幾家在報導這件事」，但不會被拿去當寫作
素材。詳細數字見 [DECISIONS](docs/DECISIONS.md)。

## 安全考量

- 只把公開來源的內容送進 LLM，走公司內網 gateway，不傳遞任何台達內部資訊
- 憑證與資料庫不進版控（見 `.gitignore`）
- 網頁預設只綁 `127.0.0.1`；要內網分享得自己加 `--host 0.0.0.0`，那樣沒有
  任何登入保護，任何連得到這台的人都看得到全部內容
