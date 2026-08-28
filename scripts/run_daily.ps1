# 每日自動出刊：抓取 → 分析 → 產出前一天的日報。
#
# 為什麼是「前一天」：排程在清晨跑，當天的文章才剛開始出現，用完整的前一天
# 資料選題才合理。要補特定日期就自己帶 -IssueDate。
#
# 2026-08-25 起建圖解凍：先確保 Neo4j 活著（scripts/start_neo4j.ps1），
# ingest 帶 --build-graph。Neo4j 起不來時 ingest_topics 會自己跳過建圖，
# 不影響日報產出（2026-08-13 修的），所以這裡不因它失敗而中止。
#
# 註冊排程（不需要管理員權限，工作只在自己帳號下跑）：
#   $t = New-ScheduledTaskTrigger -Daily -At 7:00am
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#          -Argument "-ExecutionPolicy Bypass -File C:\Users\I-cheryl.yeung\Desktop\delta-ai-newsletter\scripts\run_daily.ps1"
#   $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun
#   Register-ScheduledTask -TaskName "DeltaAI-DailyIssue" -Trigger $t -Action $a -Settings $s
#
# -StartWhenAvailable 是關鍵：筆電關機或睡眠錯過排程時，開機後會盡快補跑。
param(
    [string]$IssueDate = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd"),
    [int]$Concurrency = 8
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "runs\daily"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "$IssueDate.log"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Write-Log "=== 開始，出刊日期 $IssueDate ==="

# 步驟零：確保 Neo4j 活著（起不來只會讓建圖被跳過，不擋出刊）。
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_neo4j.ps1") 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }

# 步驟一：抓取與分析（含建圖）。失敗不要接著跑出刊，那樣只會產出半成品的一期。
Write-Log "--- ingest_topics ---"
& python -X utf8 -u -m scripts.ingest_topics --concurrency $Concurrency --build-graph 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
$ingestCode = $LASTEXITCODE
Write-Log "ingest_topics 結束，exit code $ingestCode"

if ($ingestCode -ne 0) {
    Write-Log "!! 抓取分析失敗，這次不出刊。資料已寫入的部分下次重跑會自動接續。"
    exit $ingestCode
}

# 步驟二：出刊
Write-Log "--- compose_topic_issue $IssueDate ---"
& python -X utf8 -u -m scripts.compose_topic_issue --date $IssueDate --cadence daily 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
$composeCode = $LASTEXITCODE
Write-Log "compose_topic_issue 結束，exit code $composeCode"

# 步驟三：模型排行榜快照（發佈頁用）。抓不到就算了，頁面會繼續顯示上一份，
# 不影響出刊，所以結束碼照樣用 compose 的。
Write-Log "--- fetch_leaderboard ---"
& python -X utf8 -u -m tools.fetch_leaderboard 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
Write-Log "fetch_leaderboard 結束，exit code $LASTEXITCODE"

Write-Log "=== 完成 ==="
exit $composeCode
