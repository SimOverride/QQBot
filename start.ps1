$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$pythonPath = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$botPath = Join-Path $projectDirectory 'bot.py'

try {
    Set-Location -LiteralPath $projectDirectory
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw 'Python environment is missing. Follow README.md to install it first.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory '.env') -PathType Leaf)) {
        throw 'Configuration file .env is missing. Configure it using .env.example first.'
    }

    . (Join-Path $projectDirectory 'scripts\napcat.ps1')
    $napcatConfig = Read-NapCatConfig -ProjectDirectory $projectDirectory
    Start-NapCat -Config $napcatConfig

    # 只识别本项目解释器和入口，保留其他项目的 Python 进程。
    # Windows 虚拟环境可能使用基础解释器子进程，父进程仍持有项目路径。
    $existing = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object {
            $_.ExecutablePath -ieq $pythonPath -and
            ($_.CommandLine -match ('(?i)(?:^|\s)"?' + [regex]::Escape($botPath) + '"?\s*$') -or
             $_.CommandLine -match '(?i)(?:^|\s)"?bot\.py"?\s*$')
        })
    if ($existing.Count -gt 0) {
        Write-Host "Restarting this project's QQBot. Temporary cache will be cleared. Saved history and facts are preserved."
        # 只停止已确认的项目进程及 Python 子进程，不停止 QQ 或 NapCat。
        $pythonProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'")
        foreach ($root in $existing) {
            $children = @($pythonProcesses | Where-Object { $_.ParentProcessId -eq $root.ProcessId })
            foreach ($child in $children) {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
                Wait-Process -Id $child.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
            }
            Stop-Process -Id $root.ProcessId -ErrorAction SilentlyContinue
            Wait-Process -Id $root.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
        }
    }

    Write-Host 'Starting QQBot...'
    Write-Host 'NapCat 已启动或复用；登录后会通过 OneBot 自动连接。'
    Write-Host 'Keep this window open while using the bot. Press Ctrl+C to stop.'
    & $pythonPath $botPath
    $botExitCode = $LASTEXITCODE
    Write-Host "QQBot stopped. Exit code: $botExitCode"
    if ($botExitCode -eq 3) {
        Write-Host 'If the log reports error 10048, another service occupies the configured port.'
        Write-Host 'Do not change the bot port without also updating the NapCat connection URL.'
    }
    exit $botExitCode
} catch {
    Write-Host ('[ERROR] ' + $_.Exception.Message)
    exit 1
}
