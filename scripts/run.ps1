param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".venv")) {
  & $Python -m venv .venv
}

& .\.venv\Scripts\python -m pip install --upgrade pip
& .\.venv\Scripts\pip install -r requirements.txt
& .\.venv\Scripts\python -m nicheflow_studio

