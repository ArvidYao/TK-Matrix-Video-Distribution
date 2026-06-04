@echo off
chcp 65001 >nul
title TK 视频分发工具
cd /d "%~dp0"

echo ============================================================
echo    TK 视频分发工具 v1.0.9
echo    TikTok Video Distribution Tool
echo ============================================================
echo.

REM ---- 检查 Python ----
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 未找到 Python 3.8+
    echo   请先安装 Python: https://www.python.org/downloads/
    echo   安装时请勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

REM ---- 显示 Python 版本 ----
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo   当前 Python: %%i
echo.

REM ---- 检查依赖 ----
python -c "import flask" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] 依赖未安装，正在自动安装...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] 依赖安装失败，请手动运行:
        echo   pip install -r requirements.txt
        pause
        exit /b 1
    )
)

REM ---- 检查 libimobiledevice ----
where idevice_id >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [WARN] 未检测到 idevice_id 命令
    echo   需要安装 libimobiledevice for Windows:
    echo   https://github.com/libimobiledevice-win32/imobiledevice-net/releases
    echo.
    echo   按任意键继续（仅限 Web 管理功能仍可用）...
    pause >nul
)

REM ---- 启动 Web 服务 ----
echo.
echo [INFO] 正在启动 Web 服务...
echo.

start "" http://localhost:5800

REM 使用 start /b 在后台启动 Python
start /b "" python web_app.py

echo [OK] Web 服务已启动
echo   管理页面: http://localhost:5800
echo.
echo   关闭方法: 关闭此窗口 或 按 Ctrl+C
echo.

REM 保持窗口打开
pause >nul
