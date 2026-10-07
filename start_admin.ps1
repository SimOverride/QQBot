$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$pythonPath = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$adminPath = Join-Path $projectDirectory 'admin_server.py'

# 后台独立于机器人，停止机器人后仍可迁移数据；只复用已确认的后台实例。
try {
    $ready = $false
    try {
        $session = Invoke-RestMethod -Uri 'http://127.0.0.1:8090/api/session' -TimeoutSec 2
        $ready = $session.name -eq 'QQBot 本地控制台'
    } catch { }
    if (-not $ready) {
        Start-Process -FilePath $pythonPath -ArgumentList @('"' + $adminPath + '"') -WorkingDirectory $projectDirectory -WindowStyle Hidden
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            Start-Sleep -Milliseconds 500
            try {
                $session = Invoke-RestMethod -Uri 'http://127.0.0.1:8090/api/session' -TimeoutSec 1
                if ($session.name -eq 'QQBot 本地控制台') { $ready = $true; break }
            } catch { }
        }
    }
    if (-not $ready) { throw '后台未启动，请检查 8090 端口或运行 admin_server.py 查看错误。' }
    Start-Process 'http://127.0.0.1:8090'
} catch {
    Write-Host $_.Exception.Message
    exit 1
}
