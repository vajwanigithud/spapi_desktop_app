# Edge PWA Taskbar Icon

## Why The Taskbar Showed Edge

The desktop shortcut icon is controlled by the Windows `.lnk` file. The running taskbar icon is controlled by the process/window identity.

The SP-API Desktop App currently launches through:

```text
Start_SPAPI_Desktop_App.vbs -> run_server.bat -> uvicorn -> msedge.exe --app=http://127.0.0.1:8001/
```

Because the visible window is created by `msedge.exe` in plain URL app mode, Windows can still show the Microsoft Edge icon even when the desktop shortcut has a custom icon.

## Why Installing The Edge PWA Helps

An installed Edge PWA gets its own Windows app identity, shortcut, name, and icon from the web app manifest. Launching the installed PWA shortcut gives Windows a better app identity for the taskbar and Alt+Tab than launching a plain `msedge.exe --app=` URL.

The server startup flow still stays the same. The VBS launches the BAT, the BAT starts uvicorn, and only then does the BAT launch the installed PWA shortcut if it has been configured.

## Install The Edge PWA

1. Launch the app from the `Amazon App` desktop shortcut.
2. In the Edge app window or a normal Edge browser window, open:

```text
http://127.0.0.1:8001/
```

3. Click the Edge menu `...`.
4. Open `Apps`.
5. Click `Install this site as an app`.
6. Name it:

```text
SP-API Desktop App
```

7. Complete the install.

## Find And Save The Installed PWA Shortcut

Run:

```powershell
powershell -ExecutionPolicy Bypass -File C:\spapi_desktop_app\scripts\find_edge_pwa_shortcut.ps1
```

If the installed Edge PWA shortcut is found, the script saves its path to:

```text
C:\spapi_desktop_app\config\edge_pwa_shortcut.txt
```

## Pin The Correct Taskbar Icon

1. Close the app.
2. Unpin the old Edge icon for this app from the taskbar.
3. Launch the app from the repaired `Amazon App` desktop shortcut.
4. If the running window now shows the SP-API icon, right-click it and pin it.
5. If Windows still shows the old icon, restart Explorer or clear the Windows icon cache, then launch again.

## Fallback Behavior

If `config\edge_pwa_shortcut.txt` is missing, points to a stale file, or the Edge PWA is not installed, `run_server.bat` falls back to the previous launch command:

```text
msedge.exe --app=http://127.0.0.1:8001/ --new-window
```

The app should still open even when no PWA shortcut is configured.
