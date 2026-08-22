@echo off
setlocal
chcp 65001 >nul
title v2rayN Node Checker
cd /d "%~dp0"

echo ========================================
echo   v2rayN Subscription Node Checker Web UI
echo ========================================
echo.

:: ===== 1. Locate uv (PATH or user install) =====
set "UV_EXE=uv"
uv --version >nul 2>&1
if not errorlevel 1 goto :uv_ready

:: Not in PATH, check default user install location
set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if exist "%UV_EXE%" goto :uv_ready

:: ===== 2. Auto-install uv =====
echo [INFO] uv not found on this machine.
echo [INFO] Auto-installing uv... (only uv needed, Python comes with it)
echo.
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo [ERROR] uv auto-install failed.
    echo [ERROR] Install manually, then run start.bat again:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo   Docs: https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)
if not exist "%UV_EXE%" (
    echo [ERROR] uv binary not found at %UV_EXE%
    echo [ERROR] Please restart this script or install uv manually.
    pause
    exit /b 1
)

:uv_ready
echo [OK] Using uv: %UV_EXE%
"%UV_EXE%" --version

:: ===== 3. Sync dependencies (auto-downloads Python if missing) =====
echo.
echo [1/3] Syncing dependencies...
echo [INFO] First run may download Python 3.12 automatically, please wait...
call "%UV_EXE%" sync
if errorlevel 1 (
    echo [ERROR] Dependency sync failed, check network or pyproject.toml
    pause
    exit /b 1
)

:: ===== 4. Pre-check port 5000 =====
echo [2/3] Checking port 5000...
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [WARN] Port 5000 is in use, run stop.bat first
    pause
    exit /b 1
)

:: ===== 5. Start web service =====
echo [3/3] Starting web service...
echo   Result dir: %cd%\result
echo   Visit: http://localhost:5000
echo   Press Ctrl+C to stop
echo ========================================
echo.

call "%UV_EXE%" run python app.py

echo.
echo Service exited.
pause