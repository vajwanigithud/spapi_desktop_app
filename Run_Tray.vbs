Set shell = CreateObject("WScript.Shell")
cmd = """" & "C:\spapi_desktop_app\.venv\Scripts\pythonw.exe" & """" & " " & """" & "C:\spapi_desktop_app\tray_app.py" & """"
shell.Run cmd, 0, False
