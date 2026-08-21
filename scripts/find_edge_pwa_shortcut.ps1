$ErrorActionPreference = "Stop"

$appRoot = "C:\spapi_desktop_app"
$configDir = Join-Path $appRoot "config"
$outputPath = Join-Path $configDir "edge_pwa_shortcut.txt"

$searchRoots = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Microsoft Edge Apps"),
    (Join-Path $env:USERPROFILE "Desktop"),
    "$env:PUBLIC\Desktop"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$shell = New-Object -ComObject WScript.Shell
$matches = @()
$ignored = @()

foreach ($root in $searchRoots) {
    Get-ChildItem -Path $root -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $shortcut = $shell.CreateShortcut($_.FullName)
            $name = $_.BaseName
            $target = [string]$shortcut.TargetPath
            $arguments = [string]$shortcut.Arguments
            $description = [string]$shortcut.Description
            $icon = [string]$shortcut.IconLocation
            $blob = "$name $target $arguments $description $icon"

            $hasAppName = $blob -match "SP-API|SP-API Desktop App|Amazon App"
            $hasLocalUrl = $blob -match "127\.0\.0\.1:8001|localhost:8001"
            $hasAppId = $arguments -match "--app-id="
            $isEdgeTarget = $target -match "msedge|edge_proxy|msedge_proxy"

            if ($hasAppId -and $isEdgeTarget -and ($hasAppName -or $hasLocalUrl)) {
                $score = 0
                if ($name -match "SP-API Desktop App") { $score += 50 }
                if ($name -match "SP-API") { $score += 25 }
                if ($name -match "Amazon App") { $score += 15 }
                if ($hasLocalUrl) { $score += 10 }
                $matches += [PSCustomObject]@{
                    Path = $_.FullName
                    Score = $score
                    Name = $name
                    Target = $target
                    Arguments = $arguments
                    Description = $description
                    IconLocation = $icon
                }
            } elseif ($hasAppName -or ($hasAppId -and $isEdgeTarget)) {
                $ignored += [PSCustomObject]@{
                    Path = $_.FullName
                    Target = $target
                    Arguments = $arguments
                    Reason = "Not a confirmed SP-API Edge PWA shortcut"
                }
            }
        } catch {
            Write-Verbose "Could not inspect shortcut $($_.FullName): $($_.Exception.Message)"
        }
    }
}

$best = $matches | Sort-Object Score, Path -Descending | Select-Object -First 1

if ($best -and -not [string]::IsNullOrWhiteSpace($best.Path) -and (Test-Path -LiteralPath $best.Path) -and $best.Path -match "\.lnk$") {
    if (-not (Test-Path -LiteralPath $configDir)) {
        New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    }

    Set-Content -Path $outputPath -Value $best.Path -Encoding ASCII
    Write-Host "Found Edge PWA shortcut:"
    Write-Host $best.Path
    Write-Host ""
    Write-Host "Saved shortcut path to:"
    Write-Host $outputPath
    exit 0
}

if (Test-Path -LiteralPath $outputPath) {
    Remove-Item -LiteralPath $outputPath -Force
}

Write-Host "No installed Edge PWA shortcut was found for SP-API Desktop App."
Write-Host ""
if ($ignored.Count -gt 0) {
    Write-Host "Matching shortcuts were inspected but ignored because they were not confirmed Edge PWA app-id shortcuts:"
    $ignored | Select-Object -First 8 | ForEach-Object {
        Write-Host " - $($_.Path)"
    }
    Write-Host ""
}
Write-Host "Install the Edge PWA first:"
Write-Host "1. Start the app."
Write-Host "2. In Edge app/window or Edge browser, open http://127.0.0.1:8001/"
Write-Host "3. Click the Edge menu (...)."
Write-Host "4. Open Apps."
Write-Host "5. Click Install this site as an app."
Write-Host "6. Name it SP-API Desktop App."
Write-Host "7. Run this script again:"
Write-Host "   powershell -ExecutionPolicy Bypass -File C:\spapi_desktop_app\scripts\find_edge_pwa_shortcut.ps1"
exit 1
