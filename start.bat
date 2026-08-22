@echo off
setlocal
chcp 65001 >nul
title v2rayN Node Checker
cd /d "%~dp0"

echo ========================================
echo   v2rayN Subscription Node Checker Web UI
echo ========================================
echo.

:: 1. Verify uv runtime is usable
uv --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found, install from: https://docs.astral.sh/uv/
    pause
    exit /b 1
)

:: 2. Sync dependencies
echo [1/3] Syncing dependencies...
call uv sync
if errorlevel 1 (
    echo [ERROR] Dependency sync failed, check network or pyproject.toml
    pause
    exit /b 1
)

:: 3. Pre-check port 5000
echo [2/3] Checking port 5000...
netstat -ano | findstr ":5000 " | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
    echo [WARN] Port 5000 is in use, run stop.bat first
    pause
    exit /b 1
)

:: 4. Start web service
echo [3/3] Starting web service...
echo   Result dir: %cd%\result
echo   Visit: http://localhost:5000
echo   Press Ctrl+C to stop
echo ========================================
echo.

call uv run python app.py

echo.
echo Service exited.
pause