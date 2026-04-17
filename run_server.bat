@echo off
set APP_DIR=C:\spapi_desktop_app
set VENV_PY=%APP_DIR%\.venv\Scripts\python.exe
set PORT=8001
set URL=http://127.0.0.1:%PORT%/

REM Kill anything already listening on 8001 (prevents winerror 10048)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

cd /d %APP_DIR%

REM Start uvicorn (no reload)
start "" /b "%VENV_PY%" -m uvicorn main:app --host 127.0.0.1 --port %PORT% --log-level warning

REM Wait for server
timeout /t 2 >nul

REM Open as "app window" (Edge)
start "" msedge.exe --app=%URL% --new-window
