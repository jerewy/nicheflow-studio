param(
  [string]$Python = "py",
  [string]$PythonVersion = "3.12",
  [switch]$ForceBuild,
  [switch]$InstallLocal,
  [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$exePath = Join-Path $repoRoot "dist\NicheFlowStudio\NicheFlowStudio.exe"
$buildScript = Join-Path $repoRoot "scripts\build.ps1"
$installScript = Join-Path $repoRoot "scripts\install_local.ps1"

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
    "scripts\build.ps1"
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

  if ($InstallLocal) {
    & $installScript
  }

  if (-not $NoLaunch) {
    if (!(Test-Path $exePath)) {
      throw "Packaged app not found at $exePath."
    }
    Start-Process -FilePath $exePath -WorkingDirectory (Split-Path $exePath -Parent)
  }
}
finally {
  Pop-Location
}
