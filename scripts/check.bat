@echo off
chcp 65001 >nul
cd /d "%~dp0.."

REM Fast gate before committing: syntax-check everything, then full self-test
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
echo [1/2] Byte-compile all modules...
%PYEXE% -m compileall -q main.py selftest.py roco tools
if errorlevel 1 (
    echo [FAIL] Syntax errors found.
    pause
    exit /b 1
)
echo [2/2] Running self-test...
%PYEXE% selftest.py
if errorlevel 1 pause
