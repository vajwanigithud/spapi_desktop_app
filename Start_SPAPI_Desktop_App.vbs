Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\spapi_desktop_app"
WshShell.Run """C:\spapi_desktop_app\.venv\Scripts\pythonw.exe"" ""C:\spapi_desktop_app\desktop.py""", 0, False
Set WshShell = Nothing
