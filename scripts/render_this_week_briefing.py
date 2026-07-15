"""產出本期（2026-07-15）情報簡報。

內容來源：這次是透過即時網路搜尋人工彙整（因為 Reddit API 憑證尚未設定，
config/domains.yaml 定義的自動化 pipeline 目前只能穩定抓到 arXiv 部分）。
之後 REDDIT_CLIENT_ID/SECRET 補上、且 scripts/run_pipeline.py 接上這個
briefing 版型後，就能取代這份手動彙整的腳本。

每則 entry 的 url 皆為實際可查證的來源；summary/relevance 為根據來源內容
改寫的繁中摘要，未在來源中出現的細節不編造。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from render.build_newsletter import render_briefing, save_draft

GENERAL = {
    "name": "通用AI模型趨勢",
    "eyebrow": "GENERAL AI",
    "is_general": True,
    "entries": [
        {
            "title": "【大廠】OpenAI 一週內連發 GPT-5.6 三個版本（Luna / Terra / Sol），節奏明顯加快",
            "summary": "OpenAI 於 7/9 同時發布 GPT-5.6 系列的三個變體，同日 Meta 也釋出 Muse Spark 1.1。追蹤站統計顯示近期新模型平均每 3 天就有一款發布。",
            "relevance": "模型迭代速度已進入「週更」節奏，內部若有導入LLM的專案，選型決策週期需要相應縮短。",
            "url": "https://llm-stats.com/ai-news",
            "source_type": "news",
        },
        {
            "title": "【大廠】xAI 發布 Grok 4.5，Anthropic Claude Sonnet 5 定位長文本推理",
            "summary": "Grok 4.5 於 7/8 發布；Anthropic 稍早在 6/30 推出 Claude Sonnet 5，市場定位為長文本寫作與推理任務的強力選項。",
            "relevance": "不同廠牌模型在「長任務自主執行」上的能力差異，是評估內部AI工具鏈時的重要指標。",
            "url": "https://llm-stats.com/llm-updates",
            "source_type": "news",
        },
        {
            "title": "【企業】業界觀察：推理模型用速度換精準度，效能成本大幅下降",
            "summary": "以 OpenAI o1、DeepSeek-R1 為代表的推理模型持續發展，多模態能力逐漸成為前沿模型標配，同時效率提升讓過去GPT-4等級的效能成本大幅下降。",
            "relevance": "「效能下放」意味著過去因成本卻步的企業AI應用（如批次文件分析），現在的單位成本可能已經可以重新評估。",
            "url": "https://aiflashreport.com/topics/new-ai-model-releases.html",
            "source_type": "news",
        },
        {
            "title": "【新創】新創圈模型發布觀察：7月版彙整",
            "summary": "彙整7月以來新創公司釋出的AI模型與工具，反映除了大廠外，垂直領域新創的模型發布也在加速。",
            "relevance": "部分新創模型鎖定特定產業垂直場景，可留意是否有貼近電子製造/供應鏈場景的新選項。",
            "url": "https://blog.mean.ceo/new-ai-model-releases-news-july-2026/",
            "source_type": "news",
        },
        {
            "title": "【企業】主流AI助理（ChatGPT / Gemini / Copilot）更新日誌追蹤",
            "summary": "彙整各家主力AI助理產品的版本更新紀錄，方便掌握企業常用工具背後模型/功能的異動節奏。",
            "relevance": "同仁日常若使用這些工具，版本更新常伴隨行為改變（如回應風格、工具呼叫能力），值得留意重大更新。",
            "url": "https://reconn-ai.com/llm-changelog.php",
            "source_type": "news",
        },
    ],
}

POWER_SYSTEMS = {
    "name": "電源及系統",
    "eyebrow": "零組件",
    "is_general": False,
    "entries": [
        {
            "title": "機器學習預測電源轉換器剩餘壽命（RUL）",
            "summary": "2026年發表於Scientific Reports的研究提出ML輔助框架，用以預測電源轉換器的剩餘使用壽命，精準度優於傳統方法。",
            "relevance": "若能提前預測電源模組壽命，有機會將預測性維護導入電源產品的售後服務策略。",
            "url": "https://bioengineer.org/machine-learning-predicts-power-converter-lifespan/",
            "source_type": "research",
        },
        {
            "title": "IEEE：AI驅動的電力電子轉換器控制策略",
            "summary": "IEEE會議論文探討以AI方法（含強化學習、神經網路）取代傳統控制迴路設計，處理電力電子轉換器的動態控制問題。",
            "relevance": "控制迴路是電源產品開發的核心工時之一，AI輔助控制設計的成熟度值得持續追蹤。",
            "url": "https://ieeexplore.ieee.org/document/10568766/",
            "source_type": "research",
        },
        {
            "title": "IEEE：AI優化電力電子轉換器以提升系統穩定性",
            "summary": "研究探討以AI技術優化電力電子轉換器參數，改善整體電力系統的穩定性與效能表現。",
            "relevance": "對於系統級（而非單一模組）的電源穩定性優化，此類方法可作為系統工程團隊的參考文獻。",
            "url": "https://ieeexplore.ieee.org/document/10271490/",
            "source_type": "research",
        },
        {
            "title": "arXiv：無模型深度強化學習控制電源逆變器，含知識蒸餾實作",
            "summary": "論文提出從策略學習到即時系統實作的完整流程，用知識蒸餾將複雜RL策略壓縮成可即時運行的輕量控制器。",
            "relevance": "「知識蒸餾」解法直接回應AI控制策略在嵌入式電源系統上算力有限的落地難題，技術路線值得評估。",
            "url": "https://arxiv.org/pdf/2603.07964",
            "source_type": "research",
        },
        {
            "title": "ResearchGate社群提問：電力電子轉換器中AI/ML的實際應用有哪些？",
            "summary": "工程師社群討論串彙整目前AI/ML在電力電子轉換器上的實務應用場域與侷限，屬於從業者角度的經驗交流而非論文。",
            "relevance": "可作為快速掃描「業界實際在用什麼、卡在哪裡」的入口，比單篇論文更貼近實務落地視角。",
            "url": "https://www.researchgate.net/post/Application_of_Machine_learning_Artificial_intelligence_in_power_electronics_converters",
            "source_type": "community",
        },
    ],
}

THERMAL = {
    "name": "風扇與散熱管理",
    "eyebrow": "零組件",
    "is_general": False,
    "entries": [
        {
            "title": "資料中心液冷滲透率預估從2021年3%衝到2026年37%",
            "summary": "產業趨勢報告指出液冷技術採用率在五年內大幅成長，冷卻策略持續朝液冷、AI驅動熱管理、餘熱回收方向發展。",
            "relevance": "散熱產品線若鎖定資料中心市場，液冷相關產品的優先順序可能需要重新評估。",
            "url": "https://airsysnorthamerica.com/data-center-trends-cooling-strategies-to-watch-in-2026/",
            "source_type": "news",
        },
        {
            "title": "AI資料中心液冷市場規模預估至2036年達178億美元",
            "summary": "市場報告預估AI資料中心液冷市場規模將以16.9%年複合成長率擴張，冷板式冷卻目前約佔液冷市場七成。",
            "relevance": "市場規模與成長率數字可作為散熱事業群評估液冷產品線投資優先順序的參考依據。",
            "url": "https://www.morningstar.com/news/accesswire/1163831msn/ai-datacenter-liquid-cooling-market-to-reach-usd-178-billion-by-2036-as-hyperscale-ai-infrastructure-drives-thermal-management-transformation",
            "source_type": "news",
        },
        {
            "title": "NVIDIA Rubin Ultra平台推升機櫃功率密度上看300kW",
            "summary": "研究指出新一代AI運算平台將使機櫃功率密度大幅攀升，20kW以上機櫃至少需要直接晶片液冷，50kW以上則需浸沒式或客製化液冷架構。",
            "relevance": "功率密度門檻的變化直接牽動散熱產品的技術規格路線圖，是散熱團隊需要密切關注的硬指標。",
            "url": "https://www.sciencedirect.com/science/article/abs/pii/S0306261926008329",
            "source_type": "research",
        },
        {
            "title": "LG電子於Data Center World 2026展示AI資料中心冷卻方案",
            "summary": "LG在美國Data Center World 2026展出其AI資料中心冷卻解決方案，反映終端品牌廠也積極切入液冷/散熱市場。",
            "relevance": "終端品牌廠直接跨入散熱解決方案領域，代表這個市場的競爭者版圖正在擴大，值得留意潛在客戶與競爭對手的動向重疊處。",
            "url": "https://www.lg.com/global/newsroom/news/eco-solution/lg-electronics-showcases-ai-data-center-cooling-solutions-at-data-center-world-2026/",
            "source_type": "news",
        },
        {
            "title": "2026資料中心展望：電力與散熱挑戰同列首要議題",
            "summary": "產業展望報告將電力供應與散熱列為2026年資料中心營運的兩大首要挑戰，兩者在高密度機櫃時代已高度連動。",
            "relevance": "電源與散熱議題被視為同一組問題，呼應台達「零組件」事業群內電源與散熱兩條產品線協同開發的必要性。",
            "url": "https://www.coresite.com/blog/data-center-outlook-2026-power-and-cooling-challenges-and-solutions-are-top-of-mind",
            "source_type": "news",
        },
    ],
}

EV_POWERTRAIN = {
    "name": "電動車動力系統",
    "eyebrow": "交通",
    "is_general": False,
    "entries": [
        {
            "title": "保時捷工程：AI輔助軟開關技術可將開關損耗降低達95%",
            "summary": "Porsche Engineering開發出以AI支援的智慧軟開關技術，鎖定電動車逆變器功率電晶體的開關損耗這個關鍵效率瓶頸。",
            "relevance": "95%開關損耗降低若能驗證屬實，將是電動車動力逆變器效率的重大突破，與台達的電源轉換技術高度相關。",
            "url": "https://newsroom.porsche.com/en/2026/innovation/porsche-engineering-control-center-with-ai-soft-switching-41415.html",
            "source_type": "news",
        },
        {
            "title": "Formula E賽車的AI能量管理技術如何回饋到市售車款",
            "summary": "報導探討Formula E電動方程式賽事中的AI能量管理技術，如何逐步轉化應用到一般市售電動車的能源管理策略。",
            "relevance": "賽車場域是動力系統技術的高強度試驗場，賽事技術的下放路徑值得動力系統研發團隊追蹤。",
            "url": "https://www.forbes.com/sites/jamesmorris/2026/06/06/how-formula-es-ai-revolution-is-teaching-road-cars-to-manage-energy/",
            "source_type": "news",
        },
        {
            "title": "車廠導入AI動力控制系統，透過神經網路預測並調整動力輸出",
            "summary": "部分車廠已將神經網路導入動力控制系統，即時預測駕駛狀況並調整電池到馬達的動力輸出，同時管理熱狀態與能量回收。",
            "relevance": "「預測式」動力管理是動力系統控制邏輯的方向轉變，從被動反應轉為主動預測，值得列入下一代平台的技術評估項目。",
            "url": "https://eureka.patsnap.com/report-how-to-improve-power-train-control-systems-for-ai-cars",
            "source_type": "research",
        },
        {
            "title": "Schaeffler將於CES 2026展示多項電動動力系統創新技術",
            "summary": "傳動系統供應商Schaeffler預告將在CES 2026展示多項電動動力系統相關創新，涵蓋範圍包含馬達與傳動技術。",
            "relevance": "作為動力系統領域的主要零組件供應商之一，其技術展示方向可作為市場技術風向指標。",
            "url": "https://chargedevs.com/newswire/schaeffler-to-show-multiple-electric-powertrain-innovations-at-ces-2026/",
            "source_type": "news",
        },
        {
            "title": "學術回顧：電動車AI能量管理策略的挑戰與未來方向",
            "summary": "ScienceDirect發表的回顧型論文系統性彙整目前電動車AI能量管理策略的技術路線、限制與未來研究方向。",
            "relevance": "適合作為動力系統團隊建立技術地圖的入門文獻，快速掌握目前學界已知的關鍵瓶頸。",
            "url": "https://www.sciencedirect.com/science/article/pii/S2352484725002707",
            "source_type": "research",
        },
    ],
}

INDUSTRIAL_AUTOMATION = {
    "name": "工業自動化",
    "eyebrow": "自動化",
    "is_general": False,
    "entries": [
        {
            "title": "AUTOMATE 2026於芝加哥登場，超過5萬人次與1,230家廠商參展創新高",
            "summary": "北美規模最大的自動化展會之一，聚焦機器人、AI、機器視覺、動作控制與智慧製造技術，參展規模創下歷史紀錄。",
            "relevance": "展會規模與參展廠商動向是判斷產業投資熱度的直接指標，可留意台達展區的相對聲量與競品布局。",
            "url": "https://metrology.news/automate-2026-to-spotlight-ai-robotics-and-the-future-of-industrial-automation/",
            "source_type": "news",
        },
        {
            "title": "「實體AI」成為2026年中製造業現場的主導趨勢",
            "summary": "具身機器人（embodied robotics）與代理型AI系統正將製造智慧從軟體層下放到工廠現場的實體環境，能證明實際部署成效的方案將更受青睞。",
            "relevance": "評估AI導入產線時，「能否實際落地部署」而非「概念驗證」正成為採購決策的關鍵標準。",
            "url": "https://www.marketscale.com/industries/industrial-iot/robotics-in-manufacturing-five-shifts-defining-factory-floors-in-mid-2026",
            "source_type": "news",
        },
        {
            "title": "Fanuc、Kawasaki、Stellantis建立產業AI合作夥伴關係，模仿學習與數位分身成為重點",
            "summary": "三家大廠的AI合作案例中，模仿學習（imitation learning）與數位分身技術正在改變產線設計與營運方式。",
            "relevance": "數位分身與模仿學習的組合，可作為評估台達自動化產品線是否需要強化對應能力的參考案例。",
            "url": "https://www.marketscale.com/industries/industrial-iot/us-robotics-rebound-defense-capacity-buildout-and-ai-partnerships-define-manufacturings-mid-2026-moment",
            "source_type": "news",
        },
        {
            "title": "NVIDIA贊助「人形機器人展區」登陸AUTOMATE 2026",
            "summary": "展會設立專屬人形機器人展區，多款人形機器人系統在現場進行實機展示，反映人形機器人正快速從研發走向商用展示階段。",
            "relevance": "人形機器人在產線的實用性仍在驗證階段，但展區規模顯示資本與廠商關注度已明顯升溫。",
            "url": "https://www.massrobotics.org/national-robotics-week-2026-highlights-ai-driven-automation-physical-ais-manufacturing-impact/",
            "source_type": "news",
        },
        {
            "title": "兩年內實體AI累計投資達200億美元，部分為填補技術勞力缺口而生",
            "summary": "報導指出實體AI投資快速增加，部分動能來自美國焊接技師嚴重短缺（目前缺口20萬人，十年內恐擴大到60萬人）等結構性勞力問題。",
            "relevance": "勞力缺口驅動的自動化需求屬於長期結構性趨勢，而非短期熱潮，適合納入自動化事業群的中長期規劃假設。",
            "url": "https://www.technocracy.news/ramping-up-current-state-of-robotics-in-2026/",
            "source_type": "news",
        },
    ],
}

BUILDING_AUTOMATION = {
    "name": "樓宇自動化",
    "eyebrow": "自動化",
    "is_general": False,
    "entries": [
        {
            "title": "Johnson Controls收購Nantum AI，強化OpenBlue平台的AI能源優化能力",
            "summary": "2026年4月完成收購，Nantum AI專長為透過AI演算法協助企業節省能源、強化系統控制，收購後將整合進氣側與水側應用的自主控制能力。",
            "relevance": "樓宇自動化領域的併購動作顯示大廠正積極透過收購補齊AI能源優化能力，是觀察競爭格局的重要信號。",
            "url": "https://www.johnsoncontrols.com/media-center/news/press-releases/2026/04/27/johnson-controls-acquires-nantum-ai-to-accelerate-ai-driven-energy-optimization",
            "source_type": "news",
        },
        {
            "title": "西門子於Light + Building 2026展示從智慧建築邁向自主建築的路徑",
            "summary": "西門子在展會上呈現其樓宇技術從「智慧」演進到「自主」的產品路線圖與具體案例。",
            "relevance": "「自主建築」的產品定位語言值得留意，可作為台達樓宇自動化產品線的市場定位參考。",
            "url": "https://press.siemens.com/global/en/pressrelease/siemens-showcases-journey-smart-autonomous-buildings-light-building-2026",
            "source_type": "news",
        },
        {
            "title": "AI驅動的建築智慧與法規轉型，2026年全球產業規模上看2兆美元",
            "summary": "報告預估AI驅動的建築智慧發展加上法規要求趨嚴，將推動全球住宅與建築產業在2026年創造2.03兆至2.1兆美元營收。",
            "relevance": "市場規模數字有助於評估樓宇自動化事業群在整體集團營收版圖中的成長空間與投資優先順序。",
            "url": "https://www.prnewswire.com/news-releases/ai-driven-building-intelligence-and-regulatory-transformation-set-to-reshape-the-global-homes-and-buildings-industry-in-2026-302807941.html",
            "source_type": "news",
        },
        {
            "title": "產業預測：邊緣原生、代理型AI驅動的HVAC系統將快速普及",
            "summary": "2026年產業預測指出，具備邊緣運算與代理型AI能力的空調系統將加速採用，中小型建築對能源管理系統的需求同步成長。",
            "relevance": "邊緣運算+代理型AI的組合是HVAC控制器產品規格演進的明確方向，可對照台達現有樓控產品的技術差距。",
            "url": "https://www.cohesionib.com/post/smart-building-technology-2026-outlook",
            "source_type": "news",
        },
        {
            "title": "施耐德電機觀察：技術、數據與AI正推動智慧建築與營運走向融合",
            "summary": "文章探討建築的技術系統、營運數據與AI能力如何逐漸融合，讓建築具備預期需求、優化效能、動態回應環境變化的能力。",
            "relevance": "「融合」是這篇文章的核心觀點，暗示未來樓宇自動化產品若各自為政、缺乏數據整合能力，競爭力可能受限。",
            "url": "https://blog.se.com/buildings/2026/04/09/the-future-of-smart-buildings-and-operations-how-technology-data-and-ai-are-driving-convergence/",
            "source_type": "news",
        },
    ],
}

ICT_INFRASTRUCTURE = {
    "name": "資通訊基礎設施",
    "eyebrow": "基礎設施",
    "is_general": False,
    "entries": [
        {
            "title": "2026年網路架構正式成為AI叢集的「一級公民」基礎設施",
            "summary": "產業觀察指出，Ultra Ethernet、Broadcom Tomahawk/Jericho等排程式乙太網路技術正取代專屬互連方案，支援超過10萬顆XPU的大型叢集。",
            "relevance": "網路架構規格的世代轉換，直接影響資料中心基礎設施相關產品（如電源、機櫃）的介面與規格對接需求。",
            "url": "https://nextgeninfra.io/2026-dcnetwork/",
            "source_type": "news",
        },
        {
            "title": "電力供應（而非算力）正成為AI資料中心成長的限制因素",
            "summary": "報告指出過去30-40kW的機櫃如今動輒數百kW，設計上正逼近百萬瓦等級，電力供應能力已超越算力本身，成為擴張的主要瓶頸。",
            "relevance": "這個結論直接對應台達電源與能源解決方案事業群的核心價值主張：當電力成為瓶頸，電源效率與供應能力就是勝負關鍵。",
            "url": "https://www.datacenterknowledge.com/operations-and-management/2026-predictions-ai-sparks-data-center-power-revolution",
            "source_type": "news",
        },
        {
            "title": "AI資料中心市場規模2026年達210至490億美元，預估2030年代中期成長至1,330至1,970億美元",
            "summary": "市場預估顯示AI資料中心市場將以超過25%的年複合成長率快速擴張。",
            "relevance": "高速成長的市場規模為基礎設施事業群的中長期資源投放提供量化依據。",
            "url": "https://technologychecker.io/blog/data-center-market-statistics-ai",
            "source_type": "news",
        },
        {
            "title": "全球資料中心投資版圖持續擴張，東南亞成為新興熱點",
            "summary": "2026年產業趨勢報告顯示，包含泰國在內的東南亞地區正吸引大型資料中心與雲端基礎設施投資，反映AI基礎設施建設熱潮持續向全球擴散。",
            "relevance": "新興市場的資料中心建設熱潮，可能為基礎設施事業群帶來既有市場之外的成長機會。",
            "url": "https://www.spglobal.com/energy/en/news-research/special-reports/energy-transition/2026-trends-in-data-center-services-infrastructure",
            "source_type": "news",
        },
        {
            "title": "Data Center World 2026：AI正把基礎設施推向新的極限",
            "summary": "會議report整理AI工作負載對資料中心基礎設施（電力、散熱、網路、營建時程）各層面帶來的壓力與應對方式。",
            "relevance": "這場會議的內容橫跨台達基礎設施事業群關注的所有子領域，適合作為本週優先精讀的單一來源。",
            "url": "https://www.datacenterknowledge.com/build-design/data-center-world-2026-ai-pushes-infrastructure-to-new-limits",
            "source_type": "news",
        },
    ],
}

ENERGY_SOLUTIONS = {
    "name": "電力暨能源解決方案",
    "eyebrow": "基礎設施",
    "is_general": False,
    "entries": [
        {
            "title": "AI智慧電網市場規模2026年估達75.4億美元，年複合成長率13.9%",
            "summary": "市場報告指出AI驅動的智慧電網市場正快速成長，主要應用包含負載預測優化、自癒式電網自動化、預測性資產維護分析與即時電網監控。",
            "relevance": "智慧電網相關產品規格與市場需求的成長曲線，可作為能源解決方案事業群的投資優先順序參考。",
            "url": "https://www.researchandmarkets.com/reports/6231838/ai-powered-smart-grid-market-report",
            "source_type": "news",
        },
        {
            "title": "科技巨頭砸重金投入AI電網管理、颶風與野火預測系統",
            "summary": "Google、微軟、Amazon、NVIDIA、Meta等公司在2026年持續投入數十億美元於管理智慧電網、預測極端天氣的AI系統。",
            "relevance": "科技巨頭跨界投入能源AI，代表能源解決方案的競爭者版圖已不限於傳統電力設備廠商，值得留意潛在的跨業競合關係。",
            "url": "https://finance.yahoo.com/sectors/energy/articles/ai-energy-market-report-2026-145000049.html",
            "source_type": "news",
        },
        {
            "title": "RatedPower發布2026全球再生能源趨勢報告：AI、儲能與電網限制重塑市場",
            "summary": "報告指出AI技術、儲能系統成長與電網容量限制，正共同重新定義再生能源市場的發展動態。",
            "relevance": "「電網限制」被列為與AI、儲能並列的三大變數之一，顯示單靠發電端技術已不足以解決再生能源整合問題。",
            "url": "https://www.enverus.com/newsroom/ratedpower-publishes-2026-global-renewable-energy-trends-report-as-ai-storage-and-grid-constraints-redefine-market-dynamics/",
            "source_type": "news",
        },
        {
            "title": "AI在再生能源市場的成長由智慧電網與儲能系統共同驅動",
            "summary": "報導分析AI在再生能源領域的應用成長動能，主要來自智慧電網部署與能源儲存系統的同步擴張。",
            "relevance": "儲能與電網智慧化的綁定關係，呼應台達同時布局電源轉換與能源管理解決方案的產品策略邏輯。",
            "url": "https://www.altenergymag.com/news/2026/02/02/ai-in-renewable-energy-market-driven-by-smart-grids-and-energy-storage-growth/46724/",
            "source_type": "news",
        },
        {
            "title": "能源AI五大技術趨勢：負載預測、自癒電網、預測性資產維護居首",
            "summary": "產業分析彙整目前最被看好、最有機會在五年內重塑再生能源產業的AI技術，包含負載預測優化、自癒式電網自動化與預測性維護分析。",
            "relevance": "這份技術趨勢清單可作為能源解決方案事業群規劃AI導入路線圖時的優先技術對照表。",
            "url": "https://www.startus-insights.com/innovators-guide/ai-in-energy-market-report/",
            "source_type": "news",
        },
    ],
}

VIDEO_DISPLAY = {
    "name": "視訊與顯像系統",
    "eyebrow": "基礎設施",
    "is_general": False,
    "entries": [
        {
            "title": "CVPR 2026參展規模創紀錄，118家廠商參展、逾四分之一為首次參展",
            "summary": "電腦視覺頂尖學術會議CVPR 2026吸引Tesla、Meta、Waymo等大廠參與，展會規模創下歷史新高，展現業界對視覺AI人才與技術的高度關注。",
            "relevance": "頂尖學術會議的參展熱度是觀察視覺AI人才與技術投資動向的領先指標。",
            "url": "https://cvpr.thecvf.com/Conferences/2026/News/Closing",
            "source_type": "news",
        },
        {
            "title": "CVPR 2026展示AI如何加速下一世代機器人視覺技術",
            "summary": "會議內容聚焦AI進展如何加速機器人技術從概念走向實際部署，視覺感知能力是其中的核心推進力。",
            "relevance": "視覺感知技術的進展同時牽動視訊顯像與工業自動化兩個事業群，可留意跨部門協同應用的機會。",
            "url": "https://www.roboticstomorrow.com/news/2026/04/23/cvpr-2026-showcases-how-ai-is-powering-the-next-era-of-robotics-innovation/26472/",
            "source_type": "news",
        },
        {
            "title": "Nature：以光學超穎表面實現通用型AI視覺系統",
            "summary": "研究團隊將核心電腦視覺運算直接嵌入光學超穎表面（optical metasurface），原型系統能在低功耗、裝置端即時完成多種視覺感知任務。",
            "relevance": "「光學運算取代部分電子運算」的路線若成熟，可能改變視訊顯像系統長期的功耗與架構假設，屬於前瞻技術，建議列入觀察名單而非短期評估。",
            "url": "https://www.nature.com/articles/d41586-026-01891-0",
            "source_type": "research",
        },
        {
            "title": "Visionox於MWC 2026展示AI如何重新定義顯示技術的角色",
            "summary": "Visionox在展會上發表ViP智慧像素化技術，達到69%開口率與1700+ ppi，強調顯示器正從單純的輸出裝置轉變為AI體驗的核心層。",
            "relevance": "顯示技術規格（開口率、ppi）的世代提升，是視訊與顯像系統產品線判斷技術差距的具體參考數字。",
            "url": "https://finance.yahoo.com/news/visionox-mwc-2026-ai-redefining-154000756.html",
            "source_type": "news",
        },
        {
            "title": "Superb AI奪下CVPR 2026視覺AI挑戰賽冠軍，主打免大量標註的少樣本物件偵測",
            "summary": "其視覺基礎模型ZERO在CVPR 2026少樣本物件偵測挑戰賽中奪冠，使用者可透過文字提示或範例圖片完成偵測任務，無需複雜程式開發或大量標註資料。",
            "relevance": "少樣本、低標註成本的視覺模型若成熟，可望降低視訊系統產品導入AI檢測/辨識功能的資料準備門檻。",
            "url": "https://www.morningstar.com/news/pr-newswire/20260714cn02639/superb-ai-wins-global-vision-ai-challenge-at-cvpr-2026",
            "source_type": "research",
        },
    ],
}

COLUMNS = [
    GENERAL,
    POWER_SYSTEMS,
    THERMAL,
    EV_POWERTRAIN,
    INDUSTRIAL_AUTOMATION,
    BUILDING_AUTOMATION,
    ICT_INFRASTRUCTURE,
    ENERGY_SOLUTIONS,
    VIDEO_DISPLAY,
]


def main() -> None:
    html = render_briefing(
        columns=COLUMNS,
        issue_title="台達 AI 情報簡報",
        issue_date="2026年7月15日 · 本週情報",
        intro=(
            "本期依台達四大事業群 x 8個子領域彙整本週AI相關情報，"
            "橫向瀏覽各領域重點；來源涵蓋產業新聞、學術論文與技術社群討論，"
            "點擊標題可查看原始出處。"
        ),
    )
    out = save_draft(html, output_dir="output/drafts", filename="2026-07-15-briefing.html")
    print(f"寫入完成：{out}")


if __name__ == "__main__":
    main()
