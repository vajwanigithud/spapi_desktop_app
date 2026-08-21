@echo off
setlocal EnableExtensions EnableDelayedExpansion
set APP_DIR=C:\spapi_desktop_app
set VENV_PY=%APP_DIR%\.venv\Scripts\python.exe
set PORT=8001
set URL=http://127.0.0.1:%PORT%/
set CONFIG_DIR=%APP_DIR%\config
set PWA_SHORTCUT_FILE=%CONFIG_DIR%\edge_pwa_shortcut.txt

REM Kill anything already listening on 8001 (prevents winerror 10048)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

cd /d %APP_DIR%
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%" >nul 2>&1

REM Start uvicorn (no reload)
start "" /b "%VENV_PY%" -m uvicorn main:app --host 127.0.0.1 --port %PORT% --log-level warning

REM Wait for server
timeout /t 2 >nul

REM Prefer the installed Edge PWA identity when configured.
REM This lets Windows use the installed app icon/name in Taskbar and Alt+Tab.
if exist "%PWA_SHORTCUT_FILE%" (
    set /p PWA_SHORTCUT=<"%PWA_SHORTCUT_FILE%"
    if exist "!PWA_SHORTCUT!" (
        start "" "!PWA_SHORTCUT!"
        goto :eof
    )
)

REM Fallback: open as an Edge app window exactly as before.
start "" msedge.exe --app=%URL% --new-window
