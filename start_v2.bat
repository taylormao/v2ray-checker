@echo off
chcp 936 > nul
title v2 节点检测服务 v2.1.1
pushd "%~dp0"
set PYTHONIOENCODING=utf-8
set PORT=5000
set PYEXE=.venv\Scripts\python.exe

echo ============================================================
echo    v2 节点检测服务 v2.1.1
echo ============================================================
echo.

if not exist "%PYEXE%" (
    echo [错误] 未找到虚拟环境 .venv
    echo        请先执行:  uv sync
    echo.
    popd
    pause
    exit /b 1
)

rem ---- 清理占用端口的残留进程 ----
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo [清理] 端口 %PORT% 已被进程 %%p 占用，正在结束...
    taskkill /F /PID %%p > nul 2>&1
)

if not exist "bin\sing-box.exe" (
    echo [警告] 未找到 bin\sing-box.exe
    echo        将回退到仅 TCP+TLS 模式, 结果含假阳性。
    echo.
)

echo [启动] 正在拉起服务（独立进程）...
"%PYEXE%" launcher.py
if errorlevel 1 goto FAILED

echo [等待] 正在等待服务就绪...
set /a WAITED=0
:WAITLOOP
"%PYEXE%" wait_port.py %PORT% 0.5
if %errorlevel%==0 goto WAITREADY
ping -n 2 127.0.0.1 > nul
set /a WAITED+=1
if %WAITED% lss 20 goto WAITLOOP

:FAILED
echo.
echo [失败] 服务未能启动。
echo.
echo 请查看日志文件获取详细报错：
echo     logs\server.log
echo.
echo 也可以手动运行前台查看：
echo     %PYEXE% app_v2.py
echo.
popd
pause
exit /b 1

:WAITREADY
echo.
echo [就绪] 服务已启动，耗时约 %WAITED% 秒
echo.
echo 正在打开浏览器...
start "" "http://localhost:%PORT%/"
echo.
echo 浏览器若未自动打开，手动访问：http://localhost:%PORT%/
echo.
echo -----------------------------------------
echo 本窗口可以直接关闭，服务会继续运行。
echo 要停止服务，请运行 stop_v2.bat
echo 出问题看日志：logs\server.log
echo -----------------------------------------
echo.
popd
pause
