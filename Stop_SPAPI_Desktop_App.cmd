@echo off
setlocal

rem Stop the process tree that owns the SP-API listener on port 8001.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$processIds = @(Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue ^| Select-Object -ExpandProperty OwningProcess -Unique); foreach ($processId in $processIds) { taskkill /F /T /PID $processId ^| Out-Null }"

endlocal
