@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title 同步到 GitHub · 远行商人助手

echo ============================================
echo  自测 -^> 收集改动 -^> 提交 -^> 推送 GitHub
echo ============================================
echo.
echo  可传参数，例如：
echo    scripts\sync.bat -m "fix(x): 修好了"
echo    scripts\sync.bat --tag vX.Y.Z
echo    scripts\sync.bat --dry-run        只看会提交哪些文件
echo.

rem 自测需要 tkinter：优先用 py -3（安装版通常带 tcl/tk）
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 tools\publish.py %*
) else (
    python tools\publish.py %*
)

if errorlevel 1 (
    echo.
    echo [失败] 同步未完成，请看上面的提示。
    pause
    exit /b 1
)

echo.
echo [完成] 本地与 GitHub 已一致。
pause
exit /b 0
