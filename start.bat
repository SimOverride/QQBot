@echo off
setlocal
title QQBot
pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Cannot open the QQBot project directory.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment is missing.
    echo Follow the setup instructions in README.md first.
    goto :failed
)

if not exist ".env" (
    echo [ERROR] Configuration file .env is missing.
    echo Copy .env.example to .env and configure it first.
    goto :failed
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "bot_exit_code=%errorlevel%"
popd
pause
exit /b %bot_exit_code%

:failed
popd
pause
exit /b 1
