# 檢查 scripts/ 底下每支 PowerShell 腳本：編碼有沒有 BOM、語法過不過。
#
# 為什麼需要這支（2026-10-05）：run_daily_genomics.ps1 在 9/11 被編輯後存成
# 沒有 BOM 的 UTF-8，PowerShell 5.1 於是以 ANSI 讀取，中文註解解錯碼吃掉引號
# 與大括號，排程每天都在第一行之前就以 exit 1 結束，log 一個字都沒寫。
# 這種壞法從外面看不出來（檔案在編輯器裡完全正常），只能靠檢查。
#
# 用法：powershell -ExecutionPolicy Bypass -File tools\check_ps1.ps1

$repo = Split-Path -Parent $PSScriptRoot
$bad = 0
Get-ChildItem -Path (Join-Path $repo "scripts") -Filter *.ps1 -Recurse | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$errors)
    $status = @()
    if (-not $hasBom) { $status += "缺 BOM"; $bad++ }
    if ($errors) { $status += "語法錯誤 $($errors.Count) 處"; $bad++ }
    if ($status.Count -eq 0) { $status += "OK" }
    "{0,-30} {1}" -f $_.Name, ($status -join "、")
}
if ($bad -gt 0) { "`n有 $bad 項問題，排程很可能不會跑。"; exit 1 } else { "`n全部通過。"; exit 0 }
