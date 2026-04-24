param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".venv")) {
  & $Python -m venv .venv
}

& .\.venv\Scripts\python -m pip install --upgrade pip
& .\.venv\Scripts\pip install -r requirements.txt
& .\.venv\Scripts\python -m pip install -e .

$watchCommand = ".\.venv\Scripts\python -m nicheflow_studio"
& .\.venv\Scripts\python -m watchfiles `
  --target-type command `
  --filter python `
  --grace-period 2 `
  --ignore-paths ".venv,data,.pytest_cache,src\nicheflow_studio.egg-info" `
  $watchCommand src tests
