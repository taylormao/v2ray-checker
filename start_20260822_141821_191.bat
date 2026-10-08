@echo off
chcp 65001 >nul
title v2rayN Node Checker

echo ========================================
echo   v2rayN Subscription Node Checker Web UI
echo ========================================
echo.

:: Switch to script directory
cd /d "%~dp0"

:: Check uv
where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found, install from: https://docs.astral.sh/uv/
    pause
    exit /b 1
)

:: Sync dependencies via uv
echo [1/3] Syncing dependencies...
call uv sync
if errorlevel 1 (
    echo [ERROR] Dependency sync failed, check network or pyproject.toml
    pause
    exit /b 1
)

:: Create result dir
if not exist "result" mkdir result

:: Start web service
echo [2/3] Starting web service...
echo.
echo   Result dir: %cd%\result
echo.
echo [3/3] Service started!
echo   Visit: http://localhost:5000
echo   Press Ctrl+C to stop
echo ========================================
echo.

.venv\Scripts\python.exe app.py

echo.
echo Service exited.
pause
