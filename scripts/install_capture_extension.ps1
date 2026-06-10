param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$HostExe = Join-Path $ProjectRoot 'dist\NicheFlowCaptureHost.exe'
$InstallDir = Join-Path $env:LOCALAPPDATA 'NicheFlow Studio'
$HostManifest = Join-Path $InstallDir 'com.nicheflow.capture.json'
$HostConfig = Join-Path $InstallDir 'capture-host.json'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "NicheFlow virtual environment not found at $Python"
}

Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm NicheFlowCaptureHost.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Native Messaging host build failed."
    }
}
finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$manifest = @{
    name = 'com.nicheflow.capture'
    description = 'Send the active Instagram Reel to a NicheFlow shared pool.'
    path = $HostExe
    type = 'stdio'
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($HostManifest, ($manifest | ConvertTo-Json -Depth 4), $utf8NoBom)

$config = @{
    data_dir = (Join-Path $ProjectRoot 'data')
    dotenv_path = (Join-Path $ProjectRoot '.env')
}
[System.IO.File]::WriteAllText($HostConfig, ($config | ConvertTo-Json -Depth 3), $utf8NoBom)

$registryPaths = @(
    'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.nicheflow.capture',
    'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.nicheflow.capture'
)
foreach ($registryPath in $registryPaths) {
    New-Item -Force -Path $registryPath | Out-Null
    Set-Item -Path $registryPath -Value $HostManifest
}

Write-Host "NicheFlow capture host installed for extension $ExtensionId"
Write-Host "Extension folder: $(Join-Path $ProjectRoot 'browser-extension\nicheflow-capture')"
Write-Host "Clicking the extension icon now sends the active Instagram Reel to the selected pool."
