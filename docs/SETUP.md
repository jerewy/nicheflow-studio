# Setup

How to get NicheFlow Studio running on a new Windows machine, from a fresh clone
to the first scrape. Optional parts (cloud publishing, the capture extension,
AI drafts) come after the core setup and can be skipped.

## 1. Prerequisites

- Windows 10 or 11
- Python 3.12 (3.11 also works). The `py` launcher lets you pick the version.
- Node.js 22 LTS (for building the UI)
- FFmpeg. The simplest install is `winget install Gyan.FFmpeg`. The app finds
  it on `PATH` or in the WinGet packages folder.
- An Apify account (the free plan is enough to start). Apify does the Instagram
  scraping, so you never log in to Instagram for sourcing.

## 2. Install

From the repo root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\pip install -e .
```

RapidOCR pulls in the GUI build of OpenCV, which clashes with the headless
build the app needs. Reinstall the headless build so it wins:

```powershell
.\.venv\Scripts\pip install --force-reinstall --no-deps opencv-python-headless==4.13.0.92
```

Build the UI:

```powershell
cd frontend
npm install
npm run build
cd ..
```

Only if you plan to use the local (browser-based) Instagram publisher or
Instagram login profiles, install Playwright's browser:

```powershell
.\.venv\Scripts\python -m playwright install chromium
```

## 3. Run

```powershell
.\.venv\Scripts\python -m nicheflow_studio.app.webview_app
```

`.\scripts\dev_webview.ps1` builds the UI and launches the same window in one
step. Add `-Dev` for hot reload while working on the front end.

## 4. First-run setup in the app

1. Open the **Settings** tab.
2. Under **Apify**, paste your API token (Apify console > Settings > API &
   Integrations), click **Save**, then **Check connection**.
3. Open the **Accounts** tab and add the Instagram accounts you publish to. Give
   each one a niche (for example `history`); accounts with the same niche share
   one pool of clips.
4. Pick an **Active niche** in the top-right corner. This unlocks Scraping,
   Processing, and the Dashboard.
5. On **Scraping**, add a few source profiles and run a scrape.

Keys saved on the Settings tab are stored in `settings.env` inside the data
folder (see below) and take effect immediately. If you prefer files, the same
keys can go in a `.env` file in the repo root; Settings-tab values win over
`.env`, and real environment variables win over both.

## 5. Optional: AI drafts

Titles and captions can be drafted two ways:

- **Chat path (no key):** Processing copies a prompt; paste it into any chat
  model and paste the reply back. The paste parser expects the plain-text
  `Title Option N:` / `Caption Option N:` headers the prompt asks for.
- **Groq:** add a Groq API key on the Settings tab to enable **Generate with
  Groq** and the frame-vision pass.

## 6. Optional: cloud publishing

The Cloudflare publisher posts Reels through the official Instagram Graph API on
a schedule, so your PC does not need to be on at posting time and the desktop
app never handles Instagram tokens.

You need:

- Instagram accounts switched to Professional (Business or Creator).
- A Meta developer app with the Instagram API, and a long-lived access token
  for each account.
- A free Cloudflare account.

Deploy the Worker by following `cloudflare-publisher/README.md`, then enter the
Worker URL and API key on the Settings tab and map each local account to its
Worker account key. Start in `disabled` mode and move to `validate` and `live`
one account at a time, as that README describes.

## 7. Optional: one-click capture from the browser

A Chrome/Edge extension can send the Reel you are watching straight into a
pool. See `docs/CAPTURE_EXTENSION.md`.

## Where your data lives

- Running from source: `data\` in the repo (git-ignored).
- Packaged build: `%LOCALAPPDATA%\NicheFlow Studio\data`.
- Set `NICHEFLOW_DATA_DIR` to put it somewhere else.

That folder holds the SQLite database, downloads, rendered Reels, logs, automatic
database backups, and `settings.env` with your keys. Back it up before
upgrading.

## Build a standalone app

```powershell
.\scripts\build_webview.ps1 -Windowed
```

The app lands in `dist\NicheFlowProcessing\`. Copy that whole folder to the
target PC and run `NicheFlowProcessing.exe`. FFmpeg still has to be installed
there. Leave out `-Windowed` to get a console window with tracebacks for
debugging.

## Troubleshooting

- **"APIFY_TOKEN not set"**: add the token on the Settings tab.
- **Settings shows "From a system environment variable"**: a Windows
  environment variable with the same name overrides the app. Remove it in
  Windows settings, or update it there.
- **Blank window on launch**: the UI was not built. Run `npm run build` in
  `frontend\`.
- **Export fails with an FFmpeg error**: check that `ffmpeg -version` works in a
  new PowerShell window.
- **Logs**: `logs\` inside the data folder.
