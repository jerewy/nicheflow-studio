# Installation (MVP)

For MVP we support both source-run development and a packaged Windows build.

## Steps

1. Install Python 3.11+
2. In PowerShell from the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m nicheflow_studio
```

For active development, use:

```powershell
.\scripts\dev.ps1
```

## Data Folder

- Source/dev runs write runtime files to `data/` by default (ignored by git)
- Packaged Windows builds write runtime files to `%LOCALAPPDATA%\NicheFlow Studio\data`
- Set `NICHEFLOW_DATA_DIR` to change location in either mode

## Packaged Build

Build the packaged Windows app with:

```powershell
.\scripts\build.ps1
```

The packaged executable is:

```text
dist\NicheFlowStudio\NicheFlowStudio.exe
```

Run the packaged smoke test with:

```powershell
.\scripts\smoke_packaged.ps1
```

## Packaged Update / Upgrade Expectations

Current MVP behavior is intentionally simple:

- There is no in-app updater yet
- Updating the packaged app currently means rebuilding or replacing the `dist\NicheFlowStudio\` folder with a newer build
- Packaged user data is kept outside the build output under `%LOCALAPPDATA%\NicheFlow Studio\data`

This means:

- Replacing `dist\NicheFlowStudio\` should not remove the packaged app's database, downloads, or logs
- Deleting `%LOCALAPPDATA%\NicheFlow Studio\data` will remove packaged runtime data
- If you want to preserve packaged history and downloads across app upgrades, do not delete `%LOCALAPPDATA%\NicheFlow Studio\data`

## Recommended Upgrade Flow

1. Close the packaged app.
2. Keep `%LOCALAPPDATA%\NicheFlow Studio\data` in place.
3. Replace the old `dist\NicheFlowStudio\` folder with the new packaged build.
4. Launch the new `NicheFlowStudio.exe`.
5. Run the smoke checklist in `docs/DEVELOPMENT.md` if the build changed downloader, packaging, or file-opening behavior.

## Notes About Bundled Dependencies

- The packaged app currently bundles the Python environment and the `yt-dlp` version present at build time
- If `yt-dlp` needs an update for site compatibility, update the dependency in the build environment and rebuild the packaged app
- Packaged users should not currently expect background self-updates for `yt-dlp`
