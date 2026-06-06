# Launch the migrated React Processing screen in a pywebview window.
#
#   .\scripts\dev_webview.ps1          # build frontend, then open the window (prod assets)
#   .\scripts\dev_webview.ps1 -Dev     # start the Vite dev server, then open the window (hot reload)
#
# Requires: Node/npm, the project's .venv with requirements installed
# (including pywebview), and ffmpeg on PATH for export. Set GROQ_API_KEY (or run
# Ollama) for in-app draft generation.

param([switch]$Dev)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $root "frontend"
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host "Installing frontend dependencies..."
    Push-Location $frontend
    try { npm install } finally { Pop-Location }
}

if ($Dev) {
    Write-Host "Starting Vite dev server (hot reload)..."
    $vite = Start-Process -FilePath "npm.cmd" -ArgumentList "run", "dev" `
        -WorkingDirectory $frontend -PassThru
    $env:NICHEFLOW_WEBVIEW_URL = "http://localhost:5173"
    Start-Sleep -Seconds 3
    try {
        & $py -m nicheflow_studio.app.webview_app
    } finally {
        if ($vite -and -not $vite.HasExited) { Stop-Process -Id $vite.Id -Force -ErrorAction SilentlyContinue }
        Remove-Item Env:\NICHEFLOW_WEBVIEW_URL -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "Building frontend (production assets)..."
    Push-Location $frontend
    try { npm run build } finally { Pop-Location }
    # Ensure the launcher loads the built dist, not a stale dev-server env var.
    Remove-Item Env:\NICHEFLOW_WEBVIEW_URL -ErrorAction SilentlyContinue
    & $py -m nicheflow_studio.app.webview_app
}
