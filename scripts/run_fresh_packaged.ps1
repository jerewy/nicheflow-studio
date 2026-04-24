param(
  [string]$Python = "py",
  [string]$PythonVersion = "3.12",
  [switch]$ForceBuild,
  [switch]$InstallLocal,
  [switch]$NoInstallLocal,
  [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$exePath = Join-Path $repoRoot "dist\NicheFlowStudio\NicheFlowStudio.exe"
$buildScript = Join-Path $repoRoot "scripts\build.ps1"
$installScript = Join-Path $repoRoot "scripts\install_local.ps1"
$installRoot = Join-Path $env:LOCALAPPDATA "NicheFlow Studio"
$installedAppDir = Join-Path $installRoot "app\NicheFlowStudio"
$installedExePath = Join-Path $installedAppDir "NicheFlowStudio.exe"

function Get-LatestInputWriteTime {
  $inputFiles = @()
  $recursiveInputs = @(
    "src",
    "assets"
  )
  $singleFileInputs = @(
    "requirements.txt",
    "pyproject.toml",
    "NicheFlowStudio.spec",
    "scripts\build.ps1",
    "scripts\install_local.ps1"
  )

  foreach ($relativePath in $recursiveInputs) {
    $path = Join-Path $repoRoot $relativePath
    if (Test-Path $path) {
      $inputFiles += Get-ChildItem -Path $path -File -Recurse
    }
  }

  foreach ($relativePath in $singleFileInputs) {
    $path = Join-Path $repoRoot $relativePath
    if (Test-Path $path) {
      $inputFiles += Get-Item $path
    }
  }

  if ($inputFiles.Count -eq 0) {
    throw "No build input files were found."
  }

  return ($inputFiles | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1).LastWriteTimeUtc
}

Push-Location $repoRoot
try {
  $shouldBuild = $ForceBuild -or !(Test-Path $exePath)
  if (-not $shouldBuild) {
    $latestInputWriteTime = Get-LatestInputWriteTime
    $exeWriteTime = (Get-Item $exePath).LastWriteTimeUtc
    $shouldBuild = $latestInputWriteTime -gt $exeWriteTime
  }

  if ($shouldBuild) {
    Write-Output "Packaged app is missing or stale. Rebuilding..."
    & $buildScript -Python $Python -PythonVersion $PythonVersion
  }
  else {
    Write-Output "Packaged app is up to date."
  }

  $shouldInstallLocal = (-not $NoInstallLocal) -and ($InstallLocal -or $shouldBuild -or !(Test-Path $installedExePath))
  if ((-not $shouldInstallLocal) -and (Test-Path $installedExePath)) {
    $packagedWriteTime = (Get-Item $exePath).LastWriteTimeUtc
    $installedWriteTime = (Get-Item $installedExePath).LastWriteTimeUtc
    $shouldInstallLocal = $packagedWriteTime -gt $installedWriteTime
  }

  if ($shouldInstallLocal) {
    Write-Output "Refreshing local install and shortcuts..."
    & $installScript
  }
  elseif ($NoInstallLocal) {
    Write-Output "Skipping local install refresh because -NoInstallLocal was supplied."
  }
  else {
    Write-Output "Local install and shortcuts are up to date."
  }

  if (-not $NoLaunch) {
    $launchExePath = if ((-not $NoInstallLocal) -and (Test-Path $installedExePath)) {
      $installedExePath
    }
    else {
      $exePath
    }
    if (!(Test-Path $launchExePath)) {
      throw "Packaged app not found at $launchExePath."
    }
    Start-Process -FilePath $launchExePath -WorkingDirectory (Split-Path $launchExePath -Parent)
  }
}
finally {
  Pop-Location
}
