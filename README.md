# Delta AI 趨勢日報

每天從 39 個公開來源抓 AI 相關內容，
把同一件事的多篇報導聚成「話題」，依台達各單位對應的 18 個模組打分，過門檻
的寫成中文短文出日報，每週一挑最重要的 10 則出週報。寫作素材只用話題自己
的文章、出刊前經過自檢，兩道限制都是為了擋模型瞎掰。文章裡的實體關係同步
累積成 Neo4j 知識圖譜。

三個入口對應三個階段，彼此完全脫鉤，各自可以獨立重跑：

| 入口 | 做什麼 |
|---|---|
| `scripts/ingest_topics.py` | 建池：抓取、收錄判定、聚類、標籤、發佈判定、建圖、打分 |
| `scripts/compose_topic_issue.py` | 出刊：選題、生成、自檢、存檔 |
| `scripts/serve_topics.py` | 網頁：日報、週報、領域頁、發佈頁、排行榜、知識圖譜、選題帳 |

## 系統流程

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 55, "rankSpacing": 65}}}%%
flowchart TD
    subgraph ingest["　建池：scripts.ingest_topics　"]
        SRC(["39 個來源抓取<br/>RSS、arXiv、HN、Reddit<br/>GitHub、StackExchange"])
        SRC --> DEDUP(["依 URL 去重入池"])
        DEDUP --> G1{"Gate 1<br/>收錄判定"}
        G1 -->|"太舊、非 AI"| EXC(["excluded<br/>留在池裡但什麼都不做"])
        G1 -->|"內文不到 200 字"| SIG(["signal_only<br/>只當熱度訊號<br/>不標籤、不打分"])
        G1 -->|"通過"| INC(["included 正常收錄"])

        INC --> CL{"話題聚類<br/>Qdrant 找 top 5 鄰居"}
        SIG --> CL
        CL -->|"過門檻"| MERGE(["併入既有話題<br/>跨話題時觸發合併"])
        CL -->|"灰色地帶"| LLMCHK{"問 LLM<br/>同一件事？"}
        LLMCHK -->|"是"| MERGE
        LLMCHK -->|"否、失敗"| NEW(["自成新話題"])
        CL -->|"不夠像"| NEW

        MERGE --> TAG(["標籤抽取（LLM）<br/>關鍵詞、content_mode、案例標記"])
        NEW --> TAG
        TAG --> KG[("知識圖譜（可跳過）<br/>三元組、實體解析、Neo4j")]
        TAG --> SCORE(["18 模組打分（LLM）<br/>輸入是標籤與摘要"])
    end

    subgraph compose["　出刊：scripts.compose_topic_issue　"]
        SCORE --> G2{"Gate 2<br/>候選判定"}
        G2 -->|"沒素材、沒打分"| DROP(["不進候選"])
        G2 -->|"通過"| HOT(["算熱門度<br/>報導家數 × 來源權重 × 時間衰減"])
        HOT --> SEL{"Gate 3 選題三輪<br/>模組輪動、總分遞補、保底"}
        SEL -->|"入選"| GEN(["文章生成（LLM）<br/>素材只用話題自己的文章"])
        SEL -->|"分數不夠、配額滿、版位滿"| TRACE[("落選<br/>理由寫進 selection_trace")]

        GEN --> CHK{"出刊前自檢"}
        CHK -->|"沒過"| TRACE
        CHK -->|"過"| ISSUE[("存檔成期數<br/>同天同頻率不出兩份")]
    end

    subgraph serve["　網頁：scripts.serve_topics　"]
        ISSUE --> WEB(["FastAPI + Basic Auth<br/>日報、週報、領域頁"])
        TRACE --> WEB2(["選題帳頁面<br/>為什麼選、為什麼沒選"])
        KG --> WEB3(["知識圖譜頁面"])
    end

    %% 節點角色配色：判斷點橘、LLM 步驟紫、資料落點藍、淘汰灰、產出綠
    classDef step fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#1e3a8a
    classDef gate fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f
    classDef llm fill:#ede9fe,stroke:#7c3aed,stroke-width:1.5px,color:#4c1d95
    classDef out fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
    classDef drop fill:#e2e8f0,stroke:#64748b,stroke-width:1.5px,color:#334155
    classDef store fill:#cffafe,stroke:#0891b2,stroke-width:1.5px,color:#155e75

    class SRC,DEDUP,INC,MERGE,NEW,HOT step
    class G1,CL,G2,SEL,CHK gate
    class LLMCHK,TAG,SCORE,GEN llm
    class EXC,SIG,DROP drop
    class KG,TRACE,ISSUE store
    class WEB,WEB2,WEB3 out

    style ingest fill:none,stroke:#2563eb,stroke-width:1px,stroke-dasharray:6 4
    style compose fill:none,stroke:#d97706,stroke-width:1px,stroke-dasharray:6 4
    style serve fill:none,stroke:#16a34a,stroke-width:1px,stroke-dasharray:6 4
```

節點顏色的意思：橘色菱形是判斷點、紫色是有 LLM 呼叫的步驟、圓筒是資料
落點、灰色是被擋下的去向、綠色是對讀者的出口。

## 網頁分層

上面那張圖的主角是資料，這一節換成讀者。網站是三層：首頁只負責把人分流到
對的入口，第二層是各種清單（領域、發佈、熱門、智庫、單期），第三層是單篇
全文。任何一頁都能直接連進去，不強迫從首頁開始。

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 40, "rankSpacing": 55}}}%%
flowchart TD
    HOME(["首頁　/<br/>『你想看什麼？』意圖卡"])

    subgraph L2["　第二層：清單頁　"]
        ISSUE(["單期日報<br/>/issues/{id}"])
        MOD(["領域頁<br/>/modules/{module_id}"])
        REL(["模型與工具發佈<br/>/releases"])
        HOTP(["熱門新聞<br/>/hot"])
        TT(["智庫觀察<br/>/thinktank"])
        LB(["模型排行榜<br/>/leaderboard"])
    end

    subgraph L3["　第三層：單篇　"]
        ART(["文章全文<br/>/issues/{id}/topics/{gid}"])
    end

    HOME -->|"我想看最新一期日報"| ISSUE
    HOME -->|"我想看我的事業領域／職能"| MOD
    HOME -->|"我想追大廠發佈了什麼"| REL
    HOME -->|"我想知道最近什麼最熱"| HOTP
    HOME -->|"我想看智庫怎麼分析"| TT
    HOME -->|"我想看模型排行榜"| LB

    ISSUE --> ART
    MOD --> ART
    HOTP -->|"有出刊就進全文"| ART
    HOTP -.->|"還沒寫成文章"| SRC1(["原文出處<br/>外部網站"])
    REL -.-> SRC1
    TT -.-> SRC1
    ART -.->|"原文出處"| SRC1
    ART -->|"這篇憑什麼上"| LEDGER(["選題帳<br/>入選與落選理由"])

    EDM(["EDM 信件<br/>Outlook"]) -->|"閱讀全文"| ART

    classDef l1 fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
    classDef l2 fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#1e3a8a
    classDef l3 fill:#ede9fe,stroke:#7c3aed,stroke-width:1.5px,color:#4c1d95
    classDef ext fill:#e2e8f0,stroke:#64748b,stroke-width:1.5px,color:#334155
    classDef store fill:#cffafe,stroke:#0891b2,stroke-width:1.5px,color:#155e75

    class HOME l1
    class ISSUE,MOD,REL,HOTP,TT,LB l2
    class ART l3
    class SRC1,EDM ext
    class LEDGER store

    style L2 fill:none,stroke:#2563eb,stroke-width:1px,stroke-dasharray:6 4
    style L3 fill:none,stroke:#7c3aed,stroke-width:1px,stroke-dasharray:6 4
```

虛線是離站連結（原文出處在外部網站）。EDM 信件是另一個入口，信裡已經帶
全文，點「閱讀全文」才會回到網站。

## 讀者動線

同一個網站，不同角色走的路不一樣。這是四條實際會發生的路線：

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 35, "rankSpacing": 50}}}%%
flowchart LR
    subgraph A["　事業單位同仁：追自己領域　"]
        direction LR
        A1(["早上收到 EDM"]) --> A2(["掃過導讀<br/>看哪幾則跟我有關"]) --> A3(["信裡直接讀全文"]) --> A4(["想看更多就點<br/>領域頁"])
    end

    subgraph B["　主管：看趨勢與政策　"]
        direction LR
        B1(["打開首頁"]) --> B2(["『我想看智庫怎麼分析』"]) --> B3(["讀短評與<br/>對台達的啟示"]) --> B4(["點進智庫原文"])
    end

    subgraph C["　新進同仁：不知道要看什麼　"]
        direction LR
        C1(["打開首頁"]) --> C2(["『最近什麼最熱』"]) --> C3(["看幾家在報<br/>挑最熱那則"]) --> C4(["讀全文"])
    end

    subgraph D["　編輯與工程師：查品質　"]
        direction LR
        D1(["看到某篇有疑慮"]) --> D2(["點選題帳"]) --> D3(["看分數與入選理由"]) --> D4(["對照原文出處"])
    end

    classDef entry fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
    classDef mid fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#1e3a8a
    classDef goal fill:#ede9fe,stroke:#7c3aed,stroke-width:1.5px,color:#4c1d95

    class A1,B1,C1,D1 entry
    class A2,A3,B2,B3,C2,C3,D2,D3 mid
    class A4,B4,C4,D4 goal

    style A fill:none,stroke:#16a34a,stroke-width:1px,stroke-dasharray:6 4
    style B fill:none,stroke:#2563eb,stroke-width:1px,stroke-dasharray:6 4
    style C fill:none,stroke:#d97706,stroke-width:1px,stroke-dasharray:6 4
    style D fill:none,stroke:#7c3aed,stroke-width:1px,stroke-dasharray:6 4
```

四條路線的共通點是入口不同但終點都是「原文出處」：每一則都附出處，讀者
要驗證隨時驗證得到。

## 兩條發佈管道

同一份內容出兩個版本，差別不在內容而在使用場景。

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 45, "rankSpacing": 55}}}%%
flowchart TD
    DB[("出刊資料庫<br/>issues、generated_topics")]

    DB --> EDMR(["渲染 EDM<br/>tools.render_issue_email"])
    DB --> WEBR(["網頁伺服器<br/>scripts.serve_topics"])

    EDMR --> MAIL(["Outlook 信件"])
    WEBR --> SITE(["內部網站"])

    MAIL --> M1(["信裡自帶全文<br/>不依賴伺服器開著"])
    MAIL --> M2(["每則附原文出處"])

    SITE --> S1(["歷期與領域瀏覽"])
    SITE --> S2(["熱門、發佈、智庫、排行榜"])
    SITE --> S3(["選題帳：為什麼選、為什麼沒選"])

    classDef store fill:#cffafe,stroke:#0891b2,stroke-width:1.5px,color:#155e75
    classDef step fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#1e3a8a
    classDef out fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
    classDef note fill:#f1f5f9,stroke:#64748b,stroke-width:1.5px,color:#334155

    class DB store
    class EDMR,WEBR step
    class MAIL,SITE out
    class M1,M2,S1,S2,S3 note
```

信件要能獨立存在，因為伺服器跑在筆電上、關機連結就死，所以全文直接放進
信裡；網站負責信件做不到的事：歷期、依領域瀏覽、選題帳。

## 判斷機制與門檻

門檻值全部集中在 `config/topics.yaml`，程式裡只有判斷邏輯。每個值都是拿
池裡的真實資料量出來的，調整過的值在設定檔註解留有當時的依據。目前的
主要數值：

| 機制 | 門檻 | 邏輯 |
|---|---|---|
| Gate 1 收錄 | 內文 200 字、30 天、須 AI 相關 | 短的降級成 signal_only，舊的與非 AI 的 excluded。被擋的留在池裡不刪，省的是 LLM 額度不是硬碟 |
| 話題聚類 | 相似度 0.72 | 單一連結式貪婪聚類。top 5 鄰居過門檻就併入，鄰居分屬多個話題時觸發話題合併（已出刊的除外） |
| title-only 聚類 | 門檻 0.80、灰色地帶 0.65 到 0.85 | 只有標題的文章 embedding 會塌在一起，門檻切不開，灰色地帶改問 LLM「是不是同一件事」，判不出來偏向不併 |
| 實體解析 | 灰色地帶 0.15 到 0.85 | 同一套模式：夠低直接判不同、夠高直接判相同，中間才花 LLM 額度 |
| 熱門度 | 半衰期 3.5 天 | 報導家數（不重複來源數）× 平均來源權重 × 時間衰減 |
| 選題 | 模組分下限 6.0 | 三輪：模組輪動保覆蓋、總分遞補、保底輪。content_type 與模組群各有配額，案例來源另有 tier_cap 防廠商業配吃版位 |

三個共通設計，改東西之前要知道：

1. 冪等是底線。每一步只處理「還沒做過那一步」的資料，任何一步中斷重跑
   會自動接續
2. 拒絕不等於刪除。被擋掉、落選的都留著理由碼跟數值，選題帳頁面直接讀，
   「為什麼這篇沒上」要答得出來
3. 灰色地帶模式。聚類跟實體解析都是便宜計算先分流、LLM 只判中間地帶，
   模型失敗時一律往保守方向判

## 目錄結構

| 目錄 | 內容 |
|---|---|
| `ingestion/` | 各來源抓取器。共通資料契約是 `base.py` 的 `RawItem` |
| `pipeline/` | 核心邏輯：收錄關卡（gates）、聚類（topic_clustering）、標籤（article_tagging）、發佈判定（release_check）、打分（module_scoring）、選題（topic_selection）、資料層（topic_db）、向量庫（vector_store）、翻譯、週報專欄（delta_column）、圖譜（triple_extraction、entity_resolution、graph_store） |
| `generation/` | 文章生成 |
| `review/` | 出刊前自檢 |
| `scripts/` | 三個入口＋排程用的 run_daily.ps1、run_weekly.ps1、start_neo4j.ps1 |
| `prompts/` | 所有 LLM 指令（system、user 兩段式，prompt_loader 讀） |
| `config/topics.yaml` | 唯一的設定檔：來源清單、模組定義、門檻、配額 |
| `templates/` | 網頁模板（Jinja2） |
| `tools/` | 補資料與評測工具（backfill_*、eval_*、repair_*） |
| `tests/` | 冒煙測試與固定測資 |
| `legacy/` | 已凍結的前兩條產品線，不再維護，跑不起來是刻意的 |

資料落點（都不進版控）：SQLite 在 `data/topics.db`，語意向量在 `data/qdrant`
（Qdrant embedded 模式，不用起伺服器），知識圖譜在 Neo4j，LLM 呼叫全紀錄在
`llm_logs/`（除錯時對 prompt 與回應用），排程紀錄在 `runs/`。

## 快速開始

```bash
pip install -r requirements.txt
cp .env.example .env    # 填 LLM_API_KEY、LLM_BASE_URL、NEWSLETTER_MODEL

python -m scripts.ingest_topics --concurrency 8 --build-graph   # 建池
python -m scripts.compose_topic_issue --date 2026-08-27 --cadence daily   # 出刊
python -m scripts.serve_topics    # 網頁：http://localhost:8001
```

排程走 Windows 工作排程器，錯過會補跑：

| 工作 | 時間 | 做什麼 |
|---|---|---|
| DeltaAI-DailyIssue | 每天 12:00 | `run_daily.ps1`：起 Neo4j、建池含建圖、出前一天日報 |
| DeltaAI-WeeklyIssue | 每週一 12:10 | `run_weekly.ps1`：出上週的週報，含台達專欄與大標題 |
| DeltaAI-Leaderboard | 每小時 | `run_leaderboard.ps1`：抓 LMArena 分類排行榜快照 |

內容僅供台達內部參考，請勿外流。
