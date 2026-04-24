# Development

## Prereqs

- Windows 10/11
- Python 3.11+

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m nicheflow_studio
```

If plain `python` points to an older Windows install, create the venv with a specific launcher instead:

```powershell
py -3.12 -m venv .venv
```

## Dev Runner

For iterative UI work, use the watcher-based auto-restart loop:

```powershell
.\scripts\dev.ps1
```

This watches `src/` and `tests/` for Python changes, then restarts the app automatically.

## Fresh Packaged Runner

When you need to verify the packaged `.exe` during development, use:

```powershell
.\scripts\run_fresh_packaged.ps1
```

The script checks `src/`, `assets/`, and build inputs against `dist\NicheFlowStudio\NicheFlowStudio.exe`. If the packaged app is missing or stale, it rebuilds first. It then refreshes the local installed copy and shortcuts under `%LOCALAPPDATA%\NicheFlow Studio\app\NicheFlowStudio` when needed, so the Desktop and Start Menu shortcuts keep launching the latest packaged build.

Useful options:

```powershell
.\scripts\run_fresh_packaged.ps1 -ForceBuild
.\scripts\run_fresh_packaged.ps1 -NoLaunch
.\scripts\run_fresh_packaged.ps1 -InstallLocal
.\scripts\run_fresh_packaged.ps1 -NoInstallLocal
```

`-InstallLocal` forces a local install and shortcut refresh even when the script thinks everything is current. `-NoInstallLocal` skips shortcut/install refresh and launches directly from `dist\` instead.

## Project Layout (MVP)

- `src/nicheflow_studio/` - app code
- `data/` - local runtime data (ignored by git)
- `docs/` - documentation
- `src/nicheflow_studio/queue.py` - threaded downloader queue + status updater
- `src/nicheflow_studio/app/main_window.py` - queue table with retry/open controls

## Notes

- If you hit download issues, `yt-dlp` can break when sites change; update it with:
  `.\.venv\Scripts\pip install -U yt-dlp`
- The editable install step is required because the project uses a `src/` layout

## AI Draft Setup

Processing is MVP-focused:

- The downloaded YouTube metadata provides the source title.
- The app tries to generate a local speech transcript with `faster-whisper`.
- If a smart provider is configured, the app generates title/caption options from transcript context, source title, niche settings, and sampled frames when supported.
- If transcription fails but a smart provider is configured, it falls back to metadata and sampled frame context.

For Groq-backed smart drafts:

```powershell
Copy-Item .env.example .env
# Edit .env and set GROQ_API_KEY
.\scripts\check_ai_setup.ps1
```

The setup check reports whether Groq is configured without printing the API key. It does not make a live network request.

Free/basic-safe defaults:

- `GROQ_MAX_FRAMES=3` keeps vision cost and token use low while preserving useful visual context.
- `GROQ_MONTHLY_BUDGET_USD=1.00` is the intended default budget guard for MVP-scale testing.
- `GROQ_MONTHLY_VIDEO_CAP=1000` targets roughly 1000 analyzed videos per month.
- `GROQ_DAILY_VIDEO_CAP=40` covers a 1000/month pace with some catch-up room while staying conservative.
- `GROQ_BUDGET_WARN_RATIO=0.8` means UI/batch tooling should warn at 80% of the monthly budget.
- A full visual generation uses two Groq requests per video: one vision request and one writer request.

The current code records `generation_meta.estimated_cost_usd`, token usage, and a `free-basic-safe` limit profile with each Groq-backed generation. Batch tooling should sum stored estimates before starting more generations and stop before exceeding the configured monthly budget.

## Smoke Test Checklist

Use this for a fast MVP confidence check after UI, queue, downloader, or packaging changes.

### Scenario A: Successful Shorts Download

- Launch the app from `.venv` or the packaged `.exe`
- Create/select an account if the workspace is gated
- Paste one known-good YouTube Shorts URL
- Click `Download`
- Confirm the row moves to `queued` and then `downloaded`
- Confirm the downloaded file exists under the active runtime `downloads/` folder
- Confirm `Open Video` launches the file
- Confirm `Open Folder` opens the containing folder

### Scenario B: Known Failure Case

- Paste one intentionally invalid or unsupported URL
- Click `Download`
- Confirm the app blocks obviously bad input before queueing when applicable
- If a row is created and the downloader fails, confirm the row reaches `failed`
- Confirm the error text shown in the table/detail panel is readable
- Retry the failed row only if the failure is expected to be recoverable

### Runtime Folders To Check

- Source/dev runs: `data/downloads/`
- Packaged Windows runs: `%LOCALAPPDATA%\NicheFlow Studio\data\downloads`
