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
    # 2026-10-05 從「前一天」改成當天：出刊節奏改成週一那期涵蓋週六日一，
    # 期別日期要跟出刊日一致，涵蓋範圍由 compose 按星期算（週一往回 2 天，
    # 其他天往回 1 天）。補刊時仍可用 -IssueDate 指定。
    [string]$IssueDate = (Get-Date).ToString("yyyy-MM-dd"),
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

# 先確認沒有前一次沒跑完的程序（2026-10-07 加）。
# 昨天修好 BOM 之後排程第一次真的開始跑，立刻暴露下一個問題：10/6 下午啟動的
# 那次卡在載 embedding 模型，隔天 08:54 又啟動一次，兩個程序互相鎖住資料庫，
# 結果兩天都沒出刊。有舊程序就先砍掉，再加總時限避免又卡一整天。
$stale = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*ingest_topics*' -or $_.CommandLine -like '*compose_topic_issue*' }
if ($stale) {
    foreach ($proc in $stale) {
        Write-Log ("!! 發現前一次沒跑完的程序 PID {0}，先終止：{1}" -f $proc.ProcessId, $proc.CommandLine.Substring(0, [Math]::Min(70, $proc.CommandLine.Length)))
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}

Write-Log "=== 開始，出刊日期 $IssueDate ==="

# 步驟一：抓取與分析。2026-09-11 拿掉每日建圖：建圖那步在這台無 GPU 的
# 機器上會卡在聚類/embedding 幾小時,每天排程白佔 CPU 又出不了刊。知識
# 圖譜改成有需要時手動跑 tools/backfill_graph_extraction.py。也不再每天
# 起 Neo4j。失敗不接著出刊。
Write-Log "--- ingest_topics ---"
$job = Start-Job -ScriptBlock {
    param($repo, $conc)
    Set-Location $repo
    & python -X utf8 -u -m scripts.ingest_topics --concurrency $conc 2>&1
} -ArgumentList $repo, $Concurrency
if (Wait-Job $job -Timeout 5400) {
    Receive-Job $job | ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
} else {
    Write-Log "!! 抓取超過 90 分鐘仍未完成，中止這次（下次重跑會接續已完成的部分）"
    Stop-Job $job
    Receive-Job $job | ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
    Remove-Job $job -Force
    exit 1
}
Remove-Job $job -Force
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

# 步驟四：出刊前檢查（2026-10-05 加）。測試階段維持人手寄送，所以這一步
# 不擋流程，只把該看的幾行寫進 log：版面區塊、內網位址、導讀與標題是否一致、
# 摘要是否重述標題、強度用詞有無原文依據、主編觀察的數字能不能回溯。
Write-Log "--- preflight ---"
& python -X utf8 -u -m tools.preflight_issue 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
Write-Log "preflight 結束，exit code $LASTEXITCODE"

Write-Log "=== 完成 ==="
exit $composeCode
