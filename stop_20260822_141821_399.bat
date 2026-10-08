@echo off
chcp 65001 >nul
title Stop v2rayN Checker

echo ========================================
echo   Stop v2rayN Checker
echo ========================================
echo.

:: 1. Stop checker subprocess (only match check_subscription.py)
echo [1/2] Finding checker subprocess...
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*check_subscription.py*' } | ForEach-Object { Write-Output $_.ProcessId }"`) do (
    echo   Found checker PID: %%p
    taskkill /PID %%p /F >nul 2>&1
    echo   Killed subprocess %%p
)

:: 2. Stop process occupying port 5000
echo [2/2] Releasing port 5000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do (
    echo   Found service PID: %%a
    taskkill /PID %%a /F >nul 2>&1
    echo   Port 5000 released
)

echo.
echo All related processes stopped
echo.

pause
