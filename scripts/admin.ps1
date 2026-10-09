# 启动或复用独立后台，失败时交由统一入口报告。
function Start-Admin {
    param([string]$ProjectDirectory)
    $pythonPath = Join-Path $ProjectDirectory '.venv\Scripts\python.exe'
    $adminPath = Join-Path $ProjectDirectory 'admin_server.py'
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
}
