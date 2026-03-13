# NicheFlow Studio

Multi-account content management (Windows-first MVP).

## MVP (current)

- Desktop app: PyQt6
- Local DB: SQLite (`data/nicheflow.db`)
- YouTube ingestion: `yt-dlp` (supports YouTube Shorts URLs)
- Local runtime data folder: `data/` (ignored by git)

See `docs/MVP.md` for the current scope and what’s explicitly out of scope.

## Quick Start (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m nicheflow_studio
```

Or run the helper:

```powershell
.\scripts\run.ps1
```

## Runtime Data

- Default data directory is `.\data\`
- Override with `NICHEFLOW_DATA_DIR` if you want it elsewhere

## Docs

- MVP scope: `docs/MVP.md`
- Development: `docs/DEVELOPMENT.md`
- Installation: `docs/INSTALLATION.md`
- Master plan: `PLAN.md`

