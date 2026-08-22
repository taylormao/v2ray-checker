@echo off
setlocal
chcp 65001 >nul
title Stop v2rayN Checker

echo ========================================
echo   Stop v2rayN Checker
echo ========================================
echo.

:: 1. Stop checker subprocess (only match check_subscription.py)
echo [1/2] Stopping checker subprocess...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*check_subscription.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('  Killed PID ' + $_.ProcessId) }"

:: 2. Stop service listening on port 5000 (exact match)
echo [2/2] Releasing port 5000...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; Write-Output ('  Port 5000 released, PID ' + $_) }"

echo.
echo All related processes stopped
echo.
pause