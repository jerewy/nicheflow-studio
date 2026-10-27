# Package the migrated React Processing/Accounts window into a standalone exe.
#
#   .\scripts\build_webview.ps1              # build frontend, then PyInstaller bundle
#   .\scripts\build_webview.ps1 -SkipFrontend  # reuse the existing frontend\dist
#
#   .\scripts\build_webview.ps1 -Windowed   # release build: no console window
#
# Output: dist\NicheFlowProcessing\NicheFlowProcessing.exe
#
# The default is a CONSOLE build so smoke tests print tracebacks. Use -Windowed
# for builds you hand to other people; errors still go to the log folder under
# the app's data directory.

param([switch]$SkipFrontend, [switch]$Windowed)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = ".\.venv\Scripts\python.exe"
$sidecarDir = Join-Path $root "build\tools"
$sidecar = Join-Path $sidecarDir "yt-dlp.exe"

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

New-Item -ItemType Directory -Force -Path $sidecarDir | Out-Null
if (-not (Test-Path $sidecar)) {
    Write-Host "== Downloading standalone yt-dlp sidecar =="
    Invoke-WebRequest `
        -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
        -OutFile $sidecar
} else {
    Write-Host "== Updating standalone yt-dlp sidecar =="
    & $sidecar -U
}

$consoleFlag = if ($Windowed) { "--windowed" } else { "--console" }

Write-Host "== PyInstaller bundle =="
# rapidocr_onnxruntime ships its OCR model weights as package data. Without
# --collect-all the frozen build cannot read on-screen hooks, and batches
# silently go out with no hook text.
& .\.venv\Scripts\pyinstaller.exe `
    --noconfirm --clean `
    $consoleFlag `
    --name NicheFlowProcessing `
    --paths src `
    --add-data "frontend\dist;frontend\dist" `
    --add-data "assets;assets" `
    --add-binary "$sidecar;." `
    --collect-all webview `
    --collect-all clr_loader `
    --collect-all pythonnet `
    --hidden-import clr `
    --collect-data cv2 `
    --collect-all rapidocr_onnxruntime `
    src\nicheflow_studio\app\webview_app.py

Write-Host "== Done =="
Write-Host "Run: .\dist\NicheFlowProcessing\NicheFlowProcessing.exe"
