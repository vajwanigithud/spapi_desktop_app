$ErrorActionPreference = "Stop"

# This shortcut intentionally targets the VBS launcher, not Edge directly.
# The VBS/BAT flow starts the local uvicorn server before opening the UI.
$appRoot = "C:\spapi_desktop_app"
$targetPath = Join-Path $appRoot "Start_SPAPI_Desktop_App.vbs"
$iconPath = Join-Path $appRoot "static\icons\spapi-app.ico"
$desktopShortcut = "C:\Users\visha\OneDrive\Desktop\Amazon App.lnk"
$startMenuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\SP-API Desktop App.lnk"

if (-not (Test-Path -LiteralPath $targetPath)) {
    throw "Launcher not found: $targetPath"
}

if (-not (Test-Path -LiteralPath $iconPath)) {
    throw "Icon not found: $iconPath"
}

$shell = New-Object -ComObject WScript.Shell

function Set-AppShortcut {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath
    )

    $parent = Split-Path -Parent $ShortcutPath
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $targetPath
    $shortcut.Arguments = ""
    $shortcut.WorkingDirectory = $appRoot
    $shortcut.IconLocation = $iconPath
    $shortcut.Description = "SP-API Desktop App"
    $shortcut.WindowStyle = 1
    $shortcut.Save()

    Write-Host "Updated shortcut: $ShortcutPath"
}

Set-AppShortcut -ShortcutPath $desktopShortcut
Set-AppShortcut -ShortcutPath $startMenuShortcut

Write-Host ""
Write-Host "Shortcut repair complete."
Write-Host ""
Write-Host "If the taskbar still shows the old Microsoft Edge icon:"
Write-Host "1. Unpin the old taskbar item for this app."
Write-Host "2. Run this script again."
Write-Host "3. Launch the app from the repaired Desktop or Start Menu shortcut."
Write-Host "4. Pin the newly running app window if you want it pinned."
Write-Host "5. If Windows still shows the old icon, restart Explorer or clear the Windows icon cache."
