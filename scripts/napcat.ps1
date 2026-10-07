# 联合启动配置统一从 .env 读取，只传递所需字段，不输出其他配置。
function Read-NapCatConfig {
    param([string]$ProjectDirectory)
    $python = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
    $helper = Join-Path $PSScriptRoot 'startup_config.py'
    $raw = & $python $helper $ProjectDirectory read
    if ($LASTEXITCODE -ne 0) { throw '读取 .env 启动配置失败。' }
    $config = $raw | ConvertFrom-Json
    $saveLauncher = [string]::IsNullOrWhiteSpace($config.launcher)
    if ($saveLauncher) {
        $config.launcher = (Read-Host '请输入 NapCat 启动文件完整路径（.bat、.cmd 或 .exe）').Trim().Trim('"')
    }
    # 配置只接受文件路径，不将配置内容作为 PowerShell 命令执行。
    if ([string]::IsNullOrWhiteSpace($config.launcher)) {
        throw '请在 .env 中配置 NAPCAT_LAUNCHER 启动文件路径。'
    }
    $launcher = [Environment]::ExpandEnvironmentVariables([string]$config.launcher)
    if (-not [IO.Path]::IsPathRooted($launcher)) {
        $launcher = Join-Path $ProjectDirectory $launcher
    }
    $launcher = [IO.Path]::GetFullPath($launcher)
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw 'NapCat 启动文件不存在，请检查 .env 中的 NAPCAT_LAUNCHER。'
    }
    if ([IO.Path]::GetExtension($launcher) -notin @('.bat', '.cmd', '.exe')) {
        throw 'NapCat 启动文件必须是 .bat、.cmd 或 .exe。'
    }
    # 批处理由 cmd 执行，拒绝会被其展开或改变命令结构的特殊字符。
    if ($launcher -match '[%"\r\n!]') {
        throw '启动文件路径不能包含百分号、双引号、换行或感叹号。'
    }
    $port = 0
    if (-not [int]::TryParse([string]$config.port, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw '.env 的 NAPCAT_PORT 必须是 1～65535 的本机 NapCat 监听端口。'
    }
    $config.launcher = $launcher
    $config.port = $port
    if ($saveLauncher) {
        & $python $helper $ProjectDirectory save $launcher
        if ($LASTEXITCODE -ne 0) { throw '保存 .env 启动路径失败。' }
        Write-Host '已将启动路径保存到 .env 的 NAPCAT_LAUNCHER。'
    }
    return $config
}

function Test-NapCatPort {
    param([int]$Port)
    # 只探测本机端口，不发送消息，也不将端口可用视为 QQ 已登录。
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($pending)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Start-NapCat {
    param($Config)
    if (Test-NapCatPort -Port $Config.port) {
        Write-Host 'NapCat 配置端口已监听，跳过重复启动；请确认该端口属于 NapCat。'
        return
    }
    $directory = [IO.Path]::GetDirectoryName($Config.launcher)
    # NapCat 控制台用于扫码和处理登录问题，因此明确保留交互窗口。
    if ([IO.Path]::GetExtension($Config.launcher) -in @('.bat', '.cmd')) {
        $null = Start-Process -FilePath $env:ComSpec -ArgumentList ('/d /v:off /s /c ""{0}""' -f $Config.launcher) -WorkingDirectory $directory -WindowStyle Normal -PassThru
    } else {
        $null = Start-Process -FilePath $Config.launcher -WorkingDirectory $directory -WindowStyle Normal -PassThru
    }
    Write-Host '已启动 NapCat，请在其窗口或 WebUI 完成登录。正在等待本机监听端口……'
    # 等待管理端口启动，允许启动器转交子进程后退出，不据此误判失败。
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-NapCatPort -Port $Config.port) {
            Write-Host 'NapCat 端口已就绪，继续启动机器人。QQ 登录状态以 NapCat 为准。'
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw '30 秒内未检测到 NapCat 端口。请检查启动窗口、管理员授权和 .env 的 NAPCAT_PORT；确认后重新运行 start.bat。'
}
