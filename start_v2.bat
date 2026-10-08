@echo off
chcp 936 > nul
title v2 节点检测服务
pushd "%~dp0"
set PYTHONIOENCODING=utf-8

echo ============================================================
echo    v2 节点检测服务 - 真实内核验证模式
echo    入口 app_v2.py   访问 http://localhost:5000
echo ============================================================
echo.

rem ---- 环境自检 ----
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv
    echo        请先在命令行执行:  uv sync
    echo.
    popd
    pause
    exit /b 1
)

if not exist "bin\sing-box.exe" (
    echo [警告] 未找到 bin\sing-box.exe
    echo        将回退到「仅 TCP+TLS」模式, 结果含假阳性。
    echo.
)

rem ---- 端口占用检测 ----
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
    echo [提示] 端口 5000 已被进程 %%p 占用
    echo        若界面打不开, 请先双击 stop_v2.bat 清理
    echo.
)

echo 正在启动, 请稍候...
echo.

".venv\Scripts\python.exe" app_v2.py

echo.
echo 服务已退出。
popd
pause
