# 確保 Neo4j 在跑：bolt port 沒開就用免安裝 zip 版起一個背景行程。
#
# 為什麼不是 Windows 服務：這台筆電帳號被 UAC 降權，服務安裝要跳管理員
# 確認，zip 版 console 模式不用。代價是視窗關掉就停，所以排程每次跑之前
# 都先呼叫這支確保它活著（重複呼叫無害，port 開著就直接返回）。
#
# 建圖失敗不影響出刊：ingest_topics 連不上 Neo4j 會自己跳過建圖步驟
#（2026-08-13 修的），所以這支起不來也只印警告、不丟錯誤碼。
param(
    [int]$TimeoutSeconds = 60
)

$boltPort = 7687

function Test-Bolt {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("localhost", $boltPort)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if (Test-Bolt) {
    Write-Output "[start_neo4j] Neo4j 已在跑（bolt $boltPort 有回應）。"
    exit 0
}

$env:JAVA_HOME = "C:\Users\I-cheryl.yeung\tools\jdk-21.0.12+8"
$neo4jBat = "C:\Users\I-cheryl.yeung\tools\neo4j-community-5.26.0\bin\neo4j.bat"

if (-not (Test-Path $neo4jBat)) {
    Write-Output "[start_neo4j] 找不到 $neo4jBat，跳過（建圖步驟會自動略過）。"
    exit 0
}

Write-Output "[start_neo4j] Neo4j 沒在跑，啟動中..."
Start-Process -FilePath $neo4jBat -ArgumentList "console" -WindowStyle Hidden

$elapsed = 0
while ($elapsed -lt $TimeoutSeconds) {
    Start-Sleep -Seconds 3
    $elapsed += 3
    if (Test-Bolt) {
        Write-Output "[start_neo4j] Neo4j 起來了（等了 $elapsed 秒）。"
        exit 0
    }
}
Write-Output "[start_neo4j] 等了 $TimeoutSeconds 秒還沒起來，先繼續（建圖步驟會自動略過）。"
exit 0
