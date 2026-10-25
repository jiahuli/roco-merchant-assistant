@echo off
REM Open the folder where runtime logs live (same resolution as roco.config.app_dir)
set "LOGDIR=%APPDATA%\RocoMerchant\logs"
if not exist "%LOGDIR%" (
    echo [INFO] No log directory yet at:
    echo        %LOGDIR%
    echo Logs are created on first run. You can also change the log dir in Settings.
    pause
    exit /b 1
)
explorer "%LOGDIR%"
