Set shell = CreateObject("WScript.Shell")

' Kill only processes listening on port 8001
cmd = "cmd /c for /f ""tokens=5"" %%a in ('netstat -ano ^| findstr :8001 ^| findstr LISTENING') do taskkill /F /PID %%a"

shell.Run cmd, 0, True
