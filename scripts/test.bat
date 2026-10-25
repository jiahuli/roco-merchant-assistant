@echo off
chcp 65001 >nul
cd /d "%~dp0.."

REM Run the full self-test (real windows will flash; user data is backed up and restored)
set "PYEXE="
py -3 -c "import tkinter" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py -3"
    goto :run
)
python -c "import tkinter" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    goto :run
)
if exist "C:\Users\23356\AppData\Local\Programs\Python\Python313\python.exe" (
    set "PYEXE=C:\Users\23356\AppData\Local\Programs\Python\Python313\python.exe"
    goto :run
)

echo [ERROR] No Python with tkinter found.
pause
exit /b 1

:run
%PYEXE% selftest.py
if %errorlevel% neq 0 pause
