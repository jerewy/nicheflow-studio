# Package the migrated React Processing/Accounts window into a standalone exe.
#
#   .\scripts\build_webview.ps1              # build frontend, then PyInstaller bundle
#   .\scripts\build_webview.ps1 -SkipFrontend  # reuse the existing frontend\dist
#
# Output: dist\NicheFlowProcessing\NicheFlowProcessing.exe
#
# This is a CONSOLE build (no -windowed) so the first packaged smoke test prints
# tracebacks. Switch to a windowed build once the slice is validated.

param([switch]$SkipFrontend)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = ".\.venv\Scripts\python.exe"

if (-not $SkipFrontend) {
    Write-Host "== Building frontend =="
    Push-Location frontend
    try {
        if (-not (Test-Path "node_modules")) { npm install }
        npm run build
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path "frontend\dist\index.html")) {
    throw "frontend\dist\index.html not found - build the frontend first (omit -SkipFrontend)."
}

Write-Host "== PyInstaller bundle =="
& .\.venv\Scripts\pyinstaller.exe `
    --noconfirm --clean `
    --name NicheFlowProcessing `
    --paths src `
    --add-data "frontend\dist;frontend\dist" `
    --add-data "assets;assets" `
    --collect-all webview `
    --collect-all clr_loader `
    --collect-all pythonnet `
    --hidden-import clr `
    --collect-data cv2 `
    src\nicheflow_studio\app\webview_app.py

Write-Host "== Done =="
Write-Host "Run: .\dist\NicheFlowProcessing\NicheFlowProcessing.exe"
