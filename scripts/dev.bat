@echo off
chcp 65001 >nul
cd /d "%~dp0.."

REM Pick an interpreter that has tkinter (dev machine may have several Pythons)
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
echo         Install Python 3.13 (tcl/tk enabled) or check scripts\dev.bat.
pause
exit /b 1

:run
echo Using interpreter: %PYEXE%
%PYEXE% main.py %*
if %errorlevel% neq 0 pause
