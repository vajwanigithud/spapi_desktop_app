@echo off
setlocal EnableExtensions EnableDelayedExpansion
set APP_DIR=C:\spapi_desktop_app
set VENV_PY=%APP_DIR%\.venv\Scripts\python.exe
set PORT=8001
set URL=http://127.0.0.1:%PORT%/
set CONFIG_DIR=%APP_DIR%\config
set STARTUP_LOG=%APP_DIR%\logs\startup.log

REM Kill anything already listening on 8001 (prevents winerror 10048)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

cd /d %APP_DIR%
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%" >nul 2>&1
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs" >nul 2>&1

echo [%date% %time%] Starting SP-API Desktop App on %URL% > "%STARTUP_LOG%"

REM Start uvicorn (no reload)
start "" /b "%VENV_PY%" -m uvicorn main:app --host 127.0.0.1 --port %PORT% --log-level warning >> "%STARTUP_LOG%" 2>&1

REM Wait for the server to answer before opening Edge.
for /l %%i in (1,1,30) do (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -TimeoutSec 1; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { exit 0 } } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 goto SERVER_READY
    timeout /t 1 >nul
)

echo [%date% %time%] Server did not become ready on %URL%. See logs\spapi_backend.log. >> "%STARTUP_LOG%"
start "" msedge.exe --app=%URL% --new-window
goto :eof

:SERVER_READY
echo [%date% %time%] Server is ready. Opening UI. >> "%STARTUP_LOG%"

REM Open an Edge app window for the exact URL after the backend is ready.
start "" msedge.exe --app=%URL% --new-window
