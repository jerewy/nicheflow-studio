# Installation (MVP)

For MVP we run from source on Windows.

## Steps

1. Install Python 3.11+
2. In PowerShell from the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m nicheflow_studio
```

## Data Folder

The app writes runtime files to `data/` by default (ignored by git). Set `NICHEFLOW_DATA_DIR` to change location.

