# 统一启动时重启本项目后台，确保加载当前代码。
function Start-Admin {
    param([string]$ProjectDirectory)
    $pythonPath = Join-Path $ProjectDirectory '.venv\Scripts\python.exe'
    $adminPath = Join-Path $ProjectDirectory 'admin_server.py'
    # 只停止入口与解释器都属于本项目的进程，不触碰其他服务。
    $pythonProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'")
    $existing = @($pythonProcesses | Where-Object {
        $_.ExecutablePath -ieq $pythonPath -and
        ($_.CommandLine -match ('(?i)(?:^|\s)"?' + [regex]::Escape($adminPath) + '"?\s*$') -or
         $_.CommandLine -match '(?i)(?:^|\s)"?admin_server\.py"?\s*$')
    })
    foreach ($root in $existing) {
        $children = @($pythonProcesses | Where-Object { $_.ParentProcessId -eq $root.ProcessId })
        foreach ($child in $children) {
            Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            Wait-Process -Id $child.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $root.ProcessId -ErrorAction SilentlyContinue
        Wait-Process -Id $root.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    $ready = $false
    Start-Process -FilePath $pythonPath -ArgumentList @('"' + $adminPath + '"') -WorkingDirectory $projectDirectory -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $session = Invoke-RestMethod -Uri 'http://127.0.0.1:8090/api/session' -TimeoutSec 1
            # 使用 ASCII 服务标识，避免 Windows PowerShell 5.1 解码中文导致误判。
            if ($session.service -eq 'qqbot-console') { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw '后台未启动，请检查 8090 端口或运行 admin_server.py 查看错误。' }
    Start-Process 'http://127.0.0.1:8090'
}
