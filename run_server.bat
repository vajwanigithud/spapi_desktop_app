@echo off
setlocal EnableExtensions
set "APP_DIR=C:\spapi_desktop_app"
set "VENV_PY=%APP_DIR%\.venv\Scripts\python.exe"
set "STARTUP_LOG=%APP_DIR%\logs\startup.log"
cd /d "%APP_DIR%"
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs" >nul 2>&1
echo [%date% %time%] Delegating to guarded SP-API desktop launcher. >> "%STARTUP_LOG%"
"%VENV_PY%" "%APP_DIR%\desktop.py" >> "%STARTUP_LOG%" 2>&1
