@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 远行商人助手

echo ============================================
echo  打包 远行商人助手 为单文件 exe
echo ============================================
echo.

if not exist ".buildenv\Scripts\python.exe" (
    echo [1/4] 创建独立构建环境 .buildenv ...
    python -m venv .buildenv
    if errorlevel 1 goto :err
) else (
    echo [1/4] 复用已有构建环境 .buildenv
)

echo [2/4] 安装 PyInstaller ...
".buildenv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".buildenv\Scripts\python.exe" -m pip install --quiet pyinstaller
if errorlevel 1 goto :err

echo [3/4] 生成应用图标 ...
if not exist "assets\app.ico" (
    ".buildenv\Scripts\python.exe" tools\make_icon.py assets\app.ico
    if errorlevel 1 goto :err
)

echo [4/4] 打包中（首次约 1 分钟）...
".buildenv\Scripts\pyinstaller.exe" --noconfirm app.spec
if errorlevel 1 goto :err

echo.
echo ============================================
echo  完成： dist\远行商人助手.exe
echo ============================================
echo  该 exe 可单独拷贝到任何 Windows 10/11 机器运行，
echo  无需安装 Python。
echo.
pause
exit /b 0

:err
echo.
echo [失败] 构建未完成，请查看上面的错误输出。
pause
exit /b 1
