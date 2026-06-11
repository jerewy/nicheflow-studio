param(
  [switch]$ForceBuild,
  [switch]$ResetData,
  [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$exePath = Join-Path $repoRoot "dist\NicheFlowProcessing\NicheFlowProcessing.exe"
$buildScript = Join-Path $repoRoot "scripts\build_webview.ps1"
$smokeDataDir = Join-Path $repoRoot "data\packaged-webview-smoke"

function Get-LatestInputWriteTime {
  $inputFiles = @()
  foreach ($relativePath in @("src", "assets", "frontend\src")) {
    $path = Join-Path $repoRoot $relativePath
    if (Test-Path $path) {
      $inputFiles += Get-ChildItem -Path $path -File -Recurse
    }
  }
  foreach ($relativePath in @("requirements.txt", "pyproject.toml", "scripts\build_webview.ps1")) {
    $path = Join-Path $repoRoot $relativePath
    if (Test-Path $path) {
      $inputFiles += Get-Item $path
    }
  }
  return ($inputFiles | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1).LastWriteTimeUtc
}

Push-Location $repoRoot
try {
  $shouldBuild = $ForceBuild -or !(Test-Path $exePath)
  if (-not $shouldBuild) {
    $shouldBuild = (Get-LatestInputWriteTime) -gt (Get-Item $exePath).LastWriteTimeUtc
  }
  if ($shouldBuild) {
    Write-Output "Packaged webview app is missing or stale. Rebuilding..."
    & $buildScript
  } else {
    Write-Output "Packaged webview app is up to date."
  }

  if ($ResetData -and (Test-Path $smokeDataDir)) {
    Remove-Item -LiteralPath $smokeDataDir -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $smokeDataDir | Out-Null
  Write-Output "Packaged smoke data: $smokeDataDir"

  if (-not $NoLaunch) {
    $previousDataDir = $env:NICHEFLOW_DATA_DIR
    try {
      $env:NICHEFLOW_DATA_DIR = $smokeDataDir
      Start-Process -FilePath $exePath -WorkingDirectory (Split-Path $exePath -Parent)
    } finally {
      $env:NICHEFLOW_DATA_DIR = $previousDataDir
    }
  }
}
finally {
  Pop-Location
}
