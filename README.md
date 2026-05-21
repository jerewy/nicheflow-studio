# NicheFlow Studio

Multi-account content management (Windows-first MVP).

## MVP (current)

- Desktop app: PyQt6
- Local DB: SQLite (`data/nicheflow.db`)
- YouTube ingestion: `yt-dlp` (supports YouTube Shorts URLs)
- Instagram ingestion: public Reel/post URLs download through `yt-dlp`; manual references and MP4 import remain available as fallback paths
- Local import: MP4 files can be copied into the library as already-downloaded clips
- Local runtime data folder: `data/` (ignored by git)

See `docs/MVP.md` for the current scope and what’s explicitly out of scope.

## Quick Start (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m nicheflow_studio
```

Or run the helper:

```powershell
.\scripts\run.ps1
```

For smart title/caption generation, copy `.env.example` to `.env`, set `GROQ_API_KEY`, then run:

```powershell
.\scripts\check_ai_setup.ps1
```

For the stable MVP path, paste an Instagram Reel/post URL into the main URL field and click **Download**. If Instagram blocks a URL in the future, paste it into Source Intake to save it as a manual candidate, then use **Import MP4** after manually downloading the file.

For Instagram sources that require a logged-in session, create an Instaloader session once and the app will reuse the newest `~\.instagram_scraper\*.session` file:

```powershell
instaloader --login YOUR_INSTAGRAM_USERNAME --sessionfile "$env:USERPROFILE\.instagram_scraper\YOUR_INSTAGRAM_USERNAME.session"
```

If Instaloader login is blocked by checkpoint or browser-cookie decryption, use the instagrapi probe to test an alternate Instagram session path:

```powershell
.\.venv\Scripts\python.exe scripts\instagram_instagrapi_probe.py --username YOUR_INSTAGRAM_USERNAME --target meme.ig --limit 1
```

## Development Loop

For active UI development, use the auto-restart watcher:

```powershell
.\scripts\dev.ps1
```

This watches `src/` and `tests/`, then restarts the desktop app when Python files change.

To test the packaged app during development without accidentally launching a stale exe, use:

```powershell
.\scripts\run_fresh_packaged.ps1
```

This rebuilds `dist\NicheFlowStudio\NicheFlowStudio.exe` only when app source, assets, or build inputs are newer than the existing packaged exe, then launches the packaged app.

## Packaged Windows Build

Build the packaged app with:

```powershell
.\scripts\build.ps1
```

The packaged executable is written to:

```text
dist\NicheFlowStudio\NicheFlowStudio.exe
```

Run the packaged smoke test with:

```powershell
.\scripts\smoke_packaged.ps1
```

Packaged update behavior is documented in `docs/INSTALLATION.md`. In the current MVP, upgrades are manual: replace the packaged `dist\NicheFlowStudio\` build and keep `%LOCALAPPDATA%\NicheFlow Studio\data` if you want to preserve packaged history, downloads, and logs.

## Runtime Data

- Default data directory is `.\data\`
- Override with `NICHEFLOW_DATA_DIR` if you want it elsewhere
- Packaged Windows builds default to `%LOCALAPPDATA%\NicheFlow Studio\data`

## Smoke Test Checklist

See `docs/DEVELOPMENT.md` for the current two-scenario smoke test checklist:

- successful YouTube Shorts download
- known invalid/unsupported failure case

## Docs

- MVP scope: `docs/MVP.md`
- Development: `docs/DEVELOPMENT.md`
- Installation: `docs/INSTALLATION.md`
- Master plan: `PLAN.md`
