# 每週自動出週報：每週一中午跑，涵蓋上一個完整的週日到週六
#（一週從週日起算，2026-08-25 定的；窗口由 compose_topic_issue.py 依
# --date 自動推算，這裡不用算）。
#
# 不做抓取：週報的候選池是整週日報累積下來的話題（含已上日報的，見
# scripts/compose_topic_issue.py 的說明），當天中午的日報排程已經跑過
# ingest，這裡再跑一次只是重複。
#
# 註冊排程（不需要管理員權限）：
#   $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 12:10pm
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#          -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\Users\I-cheryl.yeung\Desktop\delta-ai-newsletter\scripts\run_weekly.ps1"
#   $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun
#   Register-ScheduledTask -TaskName "DeltaAI-WeeklyIssue" -Trigger $t -Action $a -Settings $s
#
# 12:10 而不是 12:00：跟日報錯開，讓當天日報先跑完 ingest（週報不自己抓，
# 用的是日報剛更新完的池子）。-StartWhenAvailable 讓錯過的排程開機後補跑。
param(
    [string]$IssueDate = (Get-Date).ToString("yyyy-MM-dd")
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "runs\weekly"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "$IssueDate.log"

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Write-Log "=== 開始，週報出刊日期 $IssueDate ==="

# 確保 Neo4j 活著（起不來不擋出刊）。
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_neo4j.ps1") 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }

Write-Log "--- compose_topic_issue --cadence weekly ---"
& python -X utf8 -u -m scripts.compose_topic_issue --date $IssueDate --cadence weekly 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
$composeCode = $LASTEXITCODE
Write-Log "compose_topic_issue 結束，exit code $composeCode"

Write-Log "=== 完成 ==="
exit $composeCode
