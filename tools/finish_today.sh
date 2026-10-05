#!/usr/bin/env bash
# 等抓取跑完，接著重做今天這一期並開出 Outlook 草稿（2026-10-05 加）。
#
# 為什麼要串成一支：這條鏈有五步、中間兩步各要十幾分鐘，手動盯著接很容易
# 漏掉其中一步。腳本會把每一步的輸出都寫進同一份 log，中途失敗就停住，
# 不會把壞掉的結果往下帶。
#
# 用法：bash tools/finish_today.sh 12 2026-10-05 4
#   參數依序是：要取代的期號、出刊日期、候選窗口往回幾天

set -u
ISSUE_ID="${1:-12}"
ISSUE_DATE="${2:-2026-10-05}"
CARRY="${3:-4}"
cd "$(dirname "$0")/.."
LOG="runs/finish_${ISSUE_DATE}.log"
say() { echo "[finish $(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "等抓取跑完（runs/ingest_${ISSUE_DATE}c.log 出現打分完成）"
until grep -q "打分完成：成功" "runs/ingest_${ISSUE_DATE}c.log" 2>/dev/null; do
  sleep 20
done
say "抓取完成"
grep -E "本次新增|收錄判定" "runs/ingest_${ISSUE_DATE}c.log" | tail -2 | tee -a "$LOG"

say "一、備份並刪掉第 ${ISSUE_ID} 期"
python -X utf8 -u -m tools.replace_issue --issue-id "$ISSUE_ID" >> "$LOG" 2>&1 || { say "備份刪除失敗，停住"; exit 1; }

say "二、重新選題生成（窗口往回 ${CARRY} 天）"
python -X utf8 -u -m scripts.compose_topic_issue --date "$ISSUE_DATE" --cadence daily --carry-over-days "$CARRY" >> "$LOG" 2>&1 || { say "選題生成失敗，停住"; exit 1; }
grep -E "已組成|入選並成功產出|摘要重複檢查" "$LOG" | tail -3

say "三、渲染 EDM"
python -X utf8 -u -m tools.render_issue_email >> "$LOG" 2>&1 || { say "渲染失敗，停住"; exit 1; }

say "四、出刊前檢查"
python -X utf8 -u -m tools.preflight_issue >> "$LOG" 2>&1
PRE=$?
tail -16 "$LOG" | grep -E "FAIL|WARN|OK  |檢查通過|需要處理" || true
if [ $PRE -ne 0 ]; then
  say "檢查有 FAIL，草稿仍會開出來，但寄之前請先看 log"
fi

say "五、更新開場白的數字"
python -X utf8 -u tools/update_intro_numbers.py "$ISSUE_DATE" >> "$LOG" 2>&1 || say "開場白更新失敗，會用原本那份"

say "六、開 Outlook 草稿"
INTRO="runs/intro_${ISSUE_DATE}_v2.txt"
[ -f "$INTRO" ] || INTRO="runs/intro_${ISSUE_DATE}.txt"
python -X utf8 -u -m tools.draft_issue_email --intro "$INTRO" >> "$LOG" 2>&1 || { say "開草稿失敗"; exit 1; }
say "全部完成，草稿已開（開場白用 ${INTRO}）"
