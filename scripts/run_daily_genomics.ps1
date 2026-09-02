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

# 步驟零：genomics 專用 Neo4j（起不來只會讓建圖被跳過，不擋出刊）。
$neo4jUp = $false
try { $neo4jUp = (Test-NetConnection -ComputerName localhost -Port 7688 -WarningAction SilentlyContinue).TcpTestSucceeded } catch {}
if (-not $neo4jUp) {
    Write-Log "Neo4j (7688) 沒在跑，啟動中..."
    $env:JAVA_HOME = "C:\Users\I-cheryl.yeung\tools\jdk-21.0.12+8"
    Start-Process -FilePath "C:\Users\I-cheryl.yeung\tools\neo4j-genomics-5.26.0\bin\neo4j.bat" -ArgumentList "console" -WindowStyle Hidden
    Start-Sleep -Seconds 45
} else {
    Write-Log "Neo4j (7688) 已在跑。"
}

# 步驟一：抓取與分析（含建圖）。失敗不接著出刊。
Write-Log "--- ingest_topics ---"
& python -X utf8 -u -m scripts.ingest_topics --concurrency $Concurrency --build-graph 2>&1 |
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
