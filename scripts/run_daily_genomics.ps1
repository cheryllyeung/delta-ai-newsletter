# 基因檢測日報每日自動出刊（工作排程器 DeltaGenomics-DailyIssue 呼叫，07:00）。
#
# 流程：確保 genomics 專用 Neo4j（bolt 7688）活著 -> ingest 含建圖 ->
# 出前一天的日報（含 TLDR）-> 渲染 EDM 並嘗試打開 Outlook 草稿（等使用者
# 上班按傳送，全自動寄送尚未啟用）。目標是 08:00 前一切就緒。
#
# 註冊排程（不需要管理員權限）：
#   $t = New-ScheduledTaskTrigger -Daily -At 7:00am
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#          -Argument "-ExecutionPolicy Bypass -File C:\Users\I-cheryl.yeung\Desktop\delta-genomics\scripts\run_daily_genomics.ps1"
#   $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun
#   Register-ScheduledTask -TaskName "DeltaGenomics-DailyIssue" -Trigger $t -Action $a -Settings $s

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

# 步驟一：抓取與分析。2026-09-11 拿掉每日建圖：建圖那步在這台無 GPU 的
# 機器上會卡在聚類/embedding 幾小時,每天排程白佔 CPU 又出不了刊。知識
# 圖譜改成有需要時手動跑 tools/backfill_graph_extraction.py。也不再每天
# 起 Neo4j。失敗不接著出刊。
Write-Log "--- ingest_topics ---"
& python -X utf8 -u -m scripts.ingest_topics --concurrency $Concurrency 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
$ingestCode = $LASTEXITCODE
Write-Log "ingest_topics 結束，exit code $ingestCode"
if ($ingestCode -ne 0) {
    Write-Log "!! 抓取分析失敗，這次不出刊。已寫入的部分下次重跑會自動接續。"
    exit $ingestCode
}

# 步驟二：出刊（含 TLDR 與英文版）
Write-Log "--- compose_topic_issue $IssueDate ---"
& python -X utf8 -u -m scripts.compose_topic_issue --date $IssueDate --cadence daily 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
$composeCode = $LASTEXITCODE
Write-Log "compose_topic_issue 結束，exit code $composeCode"

# 步驟三：渲染 EDM 並嘗試打開 Outlook 草稿（草稿失敗不影響出刊，
# 預覽檔一定會在 runs\ 底下）。
Write-Log "--- render/draft email ---"
& python -X utf8 -u -m tools.draft_issue_email 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
Write-Log "email 步驟結束，exit code $LASTEXITCODE"

Write-Log "=== 完成 ==="
exit $composeCode
