@echo off
chcp 936 > nul
title 停止 v2 节点检测服务

echo 正在停止 v2 检测服务...
echo.

set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
    echo   结束进程 PID %%p
    taskkill /F /PID %%p > nul 2>&1
    set FOUND=1
)

rem ---- 一并清理可能残留的检测内核进程 ----
taskkill /F /IM sing-box.exe > nul 2>&1
taskkill /F /IM xray.exe > nul 2>&1

if %FOUND%==0 echo   未发现运行中的服务进程。

echo.
echo 完成。窗口将在 3 秒后关闭。
timeout /t 3 > nul
