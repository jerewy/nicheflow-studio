param(
  [string]$SourceDir = (Join-Path $PSScriptRoot "..\\dist\\NicheFlowStudio"),
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "NicheFlow Studio"),
  [switch]$NoDesktopShortcut
)

$ErrorActionPreference = "Stop"

$resolvedSource = (Resolve-Path $SourceDir).Path
$exePath = Join-Path $resolvedSource "NicheFlowStudio.exe"
if (!(Test-Path $exePath)) {
  throw "Packaged app not found at $exePath. Build the app first."
}

$appRoot = Join-Path $InstallRoot "app"
$appDir = Join-Path $appRoot "NicheFlowStudio"
$desktopPath = [Environment]::GetFolderPath("Desktop")
$programsPath = [Environment]::GetFolderPath("Programs")
$desktopShortcut = Join-Path $desktopPath "NicheFlow Studio.lnk"
$startMenuShortcut = Join-Path $programsPath "NicheFlow Studio.lnk"

New-Item -ItemType Directory -Force -Path $appRoot | Out-Null
if (Test-Path $appDir) {
  Remove-Item -Recurse -Force -LiteralPath $appDir
}
Copy-Item -Recurse -Force -LiteralPath $resolvedSource -Destination $appDir

$installedExe = Join-Path $appDir "NicheFlowStudio.exe"
$wshShell = New-Object -ComObject WScript.Shell

function New-AppShortcut {
  param(
    [string]$ShortcutPath
  )

  $shortcut = $wshShell.CreateShortcut($ShortcutPath)
  $shortcut.TargetPath = $installedExe
  $shortcut.WorkingDirectory = $appDir
  $shortcut.IconLocation = "$installedExe,0"
  $shortcut.Description = "Launch NicheFlow Studio"
  $shortcut.Save()
}

if (-not $NoDesktopShortcut) {
  New-AppShortcut -ShortcutPath $desktopShortcut
}
New-AppShortcut -ShortcutPath $startMenuShortcut

Write-Output "Installed app folder: $appDir"
if (-not $NoDesktopShortcut) {
  Write-Output "Desktop shortcut: $desktopShortcut"
}
Write-Output "Start Menu shortcut: $startMenuShortcut"
