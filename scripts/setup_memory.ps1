# 安装独立记忆引擎，不修改机器人依赖与现有 Basic Memory 全局配置。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
$engine = Join-Path $projectRoot 'data/memory-engine'
& $python -m venv $engine
if ($LASTEXITCODE -ne 0) { throw '无法创建记忆引擎环境' }
& (Join-Path $engine 'Scripts/python.exe') -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements-memory.txt')
if ($LASTEXITCODE -ne 0) { throw '记忆引擎安装失败，请检查网络和安装日志' }
Write-Host '记忆引擎安装完成；旧数据请在后台预演并完成升级。'
