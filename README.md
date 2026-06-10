# NicheFlow Studio

Multi-account content management (Windows-first MVP).

## Current Product

- Desktop app: PyQt6 today, with an incremental pywebview + React migration starting from Processing
- Local DB: SQLite (`data/nicheflow.db`)
- Backend: Python services, FFmpeg processing, Playwright publishing, and SQLite persistence
- Instagram ingestion: Apify-backed source intake plus public Reel/post download and local MP4 import
- Shared niche pools and multi-account distribution
- Instagram-ready Processing with smart drafts, editable options, export, scheduling, and publishing
- Instagram publishing: publish now, scheduled/batch publishing, automatic due-post checks, and account session health
- YouTube ingestion: secondary `yt-dlp` path, including YouTube Shorts URLs
- Local import: MP4 files can be copied into the library as already-downloaded clips
- Local runtime data folder: `data/` (ignored by git)

See `docs/MVP.md`, `docs/SOURCING_POOLING_PLAN.md`, and `docs/UI_MIGRATION_PLAN.md` for the current scope and active roadmap.

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

For the stable MVP path, paste an Instagram Reel/post URL into Source Intake to save it as a manual candidate with Apify metadata, then download or import the MP4 you want to process. The normal Instagram source scrape path also uses Apify via `APIFY_TOKEN`, so the app does not need your Instagram login cookies for scraping.

For one-click capture from Chrome or Edge directly into a shared pool, see
`docs/CAPTURE_EXTENSION.md`.

Set your Apify token in `.env`:

```
APIFY_TOKEN=apify_api_xxxxxxxxxxxxxxxxxxxx
```

Legacy Instaloader, Instagrapi, and Playwright Instagram scripts remain for explicit debugging only. Do not use them with publisher accounts.

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
- UI migration plan: `docs/UI_MIGRATION_PLAN.md`
- Sourcing/pooling plan: `docs/SOURCING_POOLING_PLAN.md`
