# 每小時抓一次 LMArena 分類排行榜快照（工作排程器 DeltaAI-Leaderboard 呼叫）。
#
# 2026-08-28 加：原本榜單跟著每日出刊排程一天抓一次，使用者要更即時的更新，
# 改成獨立的每小時排程。頁面只呈現 lmarena 原始欄位（名次／分數／票數），
# 沒有自己衍生的數字（同日曾做過「跟昨天比」的升降欄，使用者定調拿掉）。
#
# 註冊排程（不需要管理員權限，工作只在自己帳號下跑）：
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#          -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\Users\I-cheryl.yeung\Desktop\delta-ai-newsletter\scripts\run_leaderboard.ps1"
#   $t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
#          -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
#   $s = New-ScheduledTaskSettingsSet -StartWhenAvailable
#   Register-ScheduledTask -TaskName "DeltaAI-Leaderboard" -Trigger $t -Action $a -Settings $s

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "runs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "leaderboard.log"

Add-Content -Path $log -Value ("[{0}] --- fetch_leaderboard ---" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding utf8
& python -X utf8 -u -m tools.fetch_leaderboard 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
Add-Content -Path $log -Value ("[{0}] exit code {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $LASTEXITCODE) -Encoding utf8

# log 只留最後 2000 行，每小時跑不修剪會無限長大。
$lines = Get-Content $log
if ($lines.Count -gt 2000) {
    $lines | Select-Object -Last 2000 | Set-Content -Path $log -Encoding utf8
}
