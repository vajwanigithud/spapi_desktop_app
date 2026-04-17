Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\spapi_desktop_app"
WshShell.Run "cmd /c ""C:\spapi_desktop_app\run_server.bat""", 0, False
Set WshShell = Nothing
