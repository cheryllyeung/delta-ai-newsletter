# legacy：已凍結的前兩條 pipeline

這個目錄放的是 2026-08-13 以前做過、目前已經不再維護也不再執行的兩條產品線。
搬進來的原因是主線只剩話題式週報一條，但 repo 裡三條並存，打開 `scripts/`
看到二十幾支檔案，外人分不出哪一支是現在真的在跑的。

主線流程請看 [`docs/PIPELINE.md`](../docs/PIPELINE.md)。

## 這裡有什麼

| 產品線 | 設定檔 | 入口 | 做什麼 |
|---|---|---|---|
| 趨勢彙整 | `config/domains.yaml` | `scripts/run_pipeline.py` | 依台達四大事業群八個子領域抓 arXiv/Reddit，計分去重後 LLM 重寫，排版成電子報 |
| Delta Pulse | `config/pulse.yaml` | `scripts/ingest_pool.py`、`scripts/compose_issue.py`、`scripts/serve_pulse.py` | 長期文章池 + 開放式標籤 + 趨勢偵測，一篇文章一則案例 |

## 為什麼停掉

兩條都不是失敗，是被第三條取代掉：

- 趨勢彙整是「一篇文章一則內容」，同一則消息被多家報導時會重複出現，而且分類
  綁死在八個子領域，跟實際 25+ 個單位對不起來
- Delta Pulse 解決了文章池跟趨勢偵測，但分類軸只有三類，選題仍然是單篇維度
- 話題式把單位換成「話題」，分類換成 18 模組打分，這兩件事沒辦法在原本的資料
  模型上疊加，所以是重寫而不是改寫

保留的價值在於：Delta Pulse 已經實測過的 5 個案例來源、趨勢公式、自檢回灌重寫
的設計，話題式都直接沿用了，回頭要對照當初怎麼做的時候看這裡。

## 這裡的程式碼跑不起來，這是刻意的

搬進來的時候**沒有改 import 路徑**。裡面的檔案還是寫 `from pipeline.pool_db
import ...`、`load_prompt_parts("scoring")`，對應的是搬移前的目錄結構。

不改的理由：這些程式碼不會再被執行，改 import 只是讓一批死程式碼看起來像活的，
反而增加誤會。真的要跑起來對照的話，checkout 搬移前的那個 commit 最快：

```bash
git log --oneline -- legacy/   # 找搬移那次 commit
git checkout <搬移前的 commit> -- .
```

同理，`legacy/prompts/` 底下那四支 prompt（`scoring.md`／`generate.md`／
`intro.md`／`self_check.md`）也還是照舊 `prompts/` 的檔名寫死在程式裡，
`pipeline/prompt_loader.py` 現在只會去主線的 `prompts/` 找。

## 主線還在用的共用元件

搬遷時刻意留在主線、兩邊都會用到的東西：

| 元件 | 為什麼留在主線 |
|---|---|
| `ingestion/`（6 個來源 + `base.py`） | 話題式全部沿用，`RawItem` 是唯一的跨 pipeline 資料契約 |
| `pipeline/llm_client.py`、`llm_logging.py`、`prompt_loader.py` | LLM 呼叫與落地記錄的共用底層 |
| `pipeline/embeddings.py`、`text_normalize.py` | 話題式的聚類、檢索、翻譯都在用 |
