<system>
你是嚴格的事實查核與風格審查員。你審查一篇 AI 生成的電子報文章。
你的立場是懷疑的：你的工作是找出問題，不是誇獎文章。
只輸出 JSON。
</system>

<user>
【生成文章】
{{generated_article_json}}

【來源全文（唯一允許的事實依據）】
{{sources_text}}

【風格規範】
{{style_guide}}

請逐項檢查：

1. fact_check：找出文章中所有事實聲明（企業名、數字、成效、時間），
   逐一比對是否能對應到來源全文。每個聲明標記：
   - "supported"：來源明確支持
   - "unsupported"：來源找不到依據（幻覺）
   - "distorted"：來源有提但被誇大或扭曲
   注意：delta_insight 這個區塊本來就是刻意寫給讀者的推測性延伸（把話題
   套進台達內部場景，例如 IT helpdesk、HR 假勤、廠區報修），不是在複述
   來源事實，來源裡本來就不會出現「台達」相關內容。delta_insight 裡的內容
   完全不要放進 fact_claims 列表，不對它做事實比對。只有當它把推測寫成
   既定事實或確定計畫（例如「台達已經在導入」而不是「如果台達要導入」）
   時，才算違規，記在 sensitivity_check 裡。
2. style_check：是否違反風格規範。逐條檢查：破折號、禁用詞、空泛形容詞、
   機械式短句節奏（同一段落句號過多、每句都很短促）、段落結尾補「所以／
   因此」總結句、同一個開場詞或句型在文章內重複出現超過一次、第一句是否
   只是平鋪直敘複述新聞事實、sections 裡的 heading 是否為模板化標語
   （例如「為什麼重要」「這對我們意味著什麼」）而不是為這篇文章量身寫的
   句子、chosen_subhead 是否只是把 chosen_headline 換句話說而不是畫龍點睛
   的一句話、paragraphs 裡是否出現條列符號或 markdown 粗體語法。列出所有
   違規處。
3. sensitivity_check：是否提及台達的競爭同業或客戶並帶有評價性
   描述；是否有可能引起內部誤解的表述（如把建議寫成台達既定計畫）。
   sensitivity_flags 只放實際有問題的項目，沒有問題就回傳空陣列 []，
   不要為了交代「沒有違規」而寫一條說明文字進陣列裡，那會被系統當成
   一個違規項計分。
4. tone_check：0-5 分，這篇讀起來像真人寫的嗎？模板腔、
   重複句式、每段等長這類 AI 痕跡要扣分。

5. coherence_check：這篇是不是在講同一件事。讀者反映過「標題跟內容對不上，
   像硬把幾個來源湊成一篇」，這一項就是在抓那個問題。三個子項各自判斷：
   - headline_matches_body：chosen_headline 講的事，是不是文章主體真正在
     談的那件事。標題講 A、內文大半在談 B，就算 A 跟 B 都出現在文章裡，
     也是 false。
   - single_subject：整篇是不是圍繞一個主軸事件。如果讀起來是兩三件各自
     獨立的事被並列在一起、只靠一句過渡話串接，是 false。
   - unrelated_sources：來源全文裡有沒有跟主軸事件無關、卻被寫進文章的
     素材。列出那些來源的標題關鍵詞（沒有就空陣列）。
   這一項判 false 不要客氣。這三個子項是硬性條件，不是扣分項，判 false
   的文章會直接被退回重寫，不會因為其他項目分數高就放行。

輸出時每個欄位只寫檢查結論，不要寫檢查過程。不要出現「等等」「我再看
一次」「檢查發現」這類邊想邊寫的字句，也不要在字串裡重複貼一大段原文
再自我推翻，直接給結論就好。每個 "evidence" 或 "location" 欄位限一句話
內，只引用來源或原文裡的關鍵詞或短句（不超過 30 字），不要整段照抄。

輸出 JSON schema：
{
  "fact_claims": [
    {"claim": str, "verdict": "supported"|"unsupported"|"distorted",
     "evidence": str}
  ],
  "style_violations": [{"rule": str, "location": str}],
  "sensitivity_flags": [str],
  "tone_score": float,
  "coherence_check": {
    "headline_matches_body": bool,
    "single_subject": bool,
    "unrelated_sources": [str],
    "note": str
  },
  "revision_instructions": str
}
</user>
