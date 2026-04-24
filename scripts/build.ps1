param(
  [string]$Python = "py",
  [string]$PythonVersion = "3.12"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".venv")) {
  & $Python "-$PythonVersion" -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pip install -e .

& .\.venv\Scripts\pyinstaller.exe `
  --noconfirm `
  --clean `
  --windowed `
  --name NicheFlowStudio `
  --paths src `
  --add-data "assets;assets" `
  --collect-submodules yt_dlp `
  --collect-data yt_dlp `
  src\nicheflow_studio\__main__.py
