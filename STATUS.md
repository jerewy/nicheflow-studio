# NicheFlow Studio Status

Last updated: 2026-04-16

## Current Focus

Finish and harden the first real Processing workflow: better draft generation, more reliable auto-crop, readable title overlays, and faster inspection of processed outputs.

## What Is Implemented

- Desktop app boots via `python -m nicheflow_studio`
- PyQt6 main window exists with URL entry, account workspace gating, account CRUD, library table, filters, detail panel, review actions, assignment, retry, remove, open, and reveal-folder flows
- SQLite schema exists for accounts and download history, with lightweight compatibility upgrades for older DBs
- `yt-dlp` YouTube / Shorts download wrapper exists and prefers single-file MP4 output
- Background queue exists with queued/downloading/downloaded/failed states plus retry support
- Failure messages are sanitized before reaching the UI
- Run and dev PowerShell scripts exist
- Runtime data path behavior is now split correctly by context:
  - source/dev runs default to repo-local `./data`
  - `NICHEFLOW_DATA_DIR` override still works
  - packaged Windows runs are coded to default to per-user app data
- Minimal Windows packaging flow exists via PyInstaller
- One real packaged smoke test has been completed
- One real packaged YouTube download has been verified with persisted runtime data after restart
- Packaged shell-handoff code has now been inspected:
  - `Open Video` currently uses `os.startfile(file_path)`
  - `Open Folder` currently uses `os.startfile(path.parent)`
  - this means current code opens the containing folder, not true Explorer file selection
- Real packaged shell validation has now been completed manually:
  - `Open Video` works on a real packaged downloaded file
  - `Open Folder` works on a real packaged downloaded file
  - current behavior is acceptable for MVP
- Lightweight YouTube / Shorts URL validation now runs before queueing
- Clearer pre-submit messages now exist for malformed, unsupported, playlist, and channel/profile YouTube URLs
- Minimum useful duplicate protection now blocks same-video resubmits per account across watch/share/shorts URL variants
- Common downloader-side `yt-dlp` failures now map to clearer UI messages
- A small maintainability pass has reduced repeated status/toast/refresh logic in `main_window.py` without changing workflow behavior
- The library UI now surfaces stored extractor/video-id metadata in the detail panel and search flow
- Account records now store scraping/intake configuration:
  - YouTube source URLs
  - max intake items
  - max candidate age in days
- A YouTube scraping/intake module now exists at `src/nicheflow_studio/scraper/youtube.py`
- Scraped candidates now persist separately from download history in a `scrape_candidates` table
- The main UI now includes a source-intake panel that can:
  - fetch candidates for the selected account
  - show candidate rows
  - ignore candidates
  - queue one selected candidate into the existing download flow
- Scraping is now source-driven with first-class `Source` and `ScrapeRun` records
- Scrape execution now runs in a background worker with live progress updates, so the UI stays responsive during source fetches
- The app now uses a sidebar-based shell with separate module pages for:
  - Scraping
  - Downloads
  - Processing
  - Uploads
  - Accounts
- The sidebar rail has now been refreshed for better compact usability:
  - the old boxed `NF` tile has been replaced with a passive `NicheFlow` brand label
  - the compact rail width is now widened slightly to avoid clipped icons
  - the active navigation item now exposes a clear selected state
  - the bottom account-toggle control is anchored cleanly at the end of the rail
- Account management now has its own page destination instead of being mixed into the main workspace flow
- Candidate intake now has stronger visual review cues:
  - color-coded candidate states
  - a candidate-state filter
  - a taller candidate table for easier scanning
- The scraping page is now split into focused tabs for:
  - Sources
  - Candidates
  - Runs
- Source management is now more structured:
  - filter by all/enabled/disabled
  - sort by priority/status/last scraped/label
  - enable/disable directly from a row dropdown
- A visible scrape progress bar now shows source-level progress while scraping runs
- Intake source URLs now normalize channel/profile subpages like `/@name/shorts` to the channel/profile root
- Library filtering now keeps unassigned downloads visible so preserved files do not disappear from the UI
- Removing a download history row now resets any linked scrape candidates so they can be queued again later
- Candidate review semantics are now clearer in the UI:
  - `candidate` displays as `ready`
  - `Ignore For Now` is reversible through `Return To Review`
  - selected candidates show a state-specific action hint
- Download review semantics are now closer to the scraping workflow:
  - `new` displays as `ready`
  - `rejected` displays as `ignored`
  - download review actions now use clearer labels like `Keep For This Account`, `Ignore From Library`, and `Return To Review`
  - the Downloads detail panel now shows a state-specific review hint
- Downloads now support batch-safe review actions:
  - multi-row selection is enabled in the library table
  - `Keep Selected`, `Ignore Selected`, and `Return Selected To Review` are available
  - batch actions are limited to review-state updates only, not delete/retry/file actions
- Scrape intake duplicate handling is now account-scoped again:
  - the same YouTube video is deduped within the current account
  - same-account re-scrapes still refresh the existing candidate row instead of duplicating it
  - the same source video is allowed in a different account when accounts intentionally overlap
- A reusable two-scenario smoke checklist now exists in `docs/DEVELOPMENT.md`
- Packaged update/upgrade expectations are now documented in `docs/INSTALLATION.md`
- The Accounts page now includes runtime-management tools:
  - visible runtime paths for data, DB, downloads, logs, and backups
  - local backup zip creation
  - restore from a supplied backup zip path or the latest backup
- The Processing page now includes:
  - source preview with full-duration playback controls
  - processed-output preview mode
  - latest-output visibility and direct open
  - clearer generation/loading progress
  - transcript + smart draft generation only for the chosen video
  - three editable smart draft options instead of four
- Processing smart generation now uses richer context:
  - local transcript when available
  - sampled video frames for visual grounding
  - provider fallback plus deterministic local fallback when hosted output is unusable
- Processing export now supports:
  - title-only rendered output
  - automatic crop suggestion
  - dark title-band / blank-band trimming when detected
  - adaptive title sizing and wrapping so narrow outputs do not get oversized headers
- Automated tests exist for path setup, DB initialization/compatibility, queue behavior, and major UI flows

## What In The Previous Status Was Stale Or Inaccurate

- The repo no longer lacks a packaged-runtime path policy; that behavior is now implemented in `src/nicheflow_studio/core/paths.py`
- “Decide the packaged Windows default data directory” is no longer the next task; the remaining work is to package around the implemented path behavior and validate it in a real frozen build
- The earlier status implied path behavior was the main unresolved runtime question; that is narrower now

## What Is Not Yet Proven

- The latest Processing crop/title behavior has not yet been manually re-checked against a broader set of real videos
- The new dark-band trimming behavior has not yet been manually validated across different Shorts layouts
- Smart draft quality is improved but still not yet proven across multiple niches and silent-video cases
- Manual retry/remove/selection-persistence/refresh behavior has not yet been explicitly re-checked in the packaged workflow
- Candidate ingestion has not yet been manually validated against multiple real-world YouTube channel/profile sources
- The refined Accounts page, source tabs, progress bar, candidate-action hints, and unassigned-download workflow have not yet been manually re-checked in a full daily-use pass
- The aligned Downloads review labels and detail-panel hint have not yet been manually re-checked in a full daily-use pass
- The new batch-safe Downloads review actions have not yet been manually re-checked in a full daily-use pass
- The new backup restore flow has not yet been manually re-checked through the Accounts page in a full daily-use pass
- The packaged `.exe` in `dist/` has not yet been re-verified after the latest sidebar rail refresh

## Notes On Verification

- Focused path tests now exist for:
  - source/dev default path behavior
  - `NICHEFLOW_DATA_DIR` override behavior
  - simulated packaged Windows default path behavior
  - override precedence even under simulated packaged runtime
- Queue tests still mock the downloader, so they validate queue state handling more than live `yt-dlp` compatibility
- UI tests still run offscreen and do not prove packaged Windows shell integration
- Submit-path tests now cover accepted and rejected YouTube / Shorts URL cases plus duplicate-prevention behavior
- Queue tests now cover clearer failure-message mapping for common `yt-dlp` error cases
- Code inspection shows the current shell behavior is simple Windows `os.startfile(...)` handoff rather than any packaged-specific integration layer
- `scripts/smoke_packaged.ps1 -KeepRuntimeData` now prepares a real packaged runtime item and prints the packaged file path/metadata for manual shell checks
- Scraping/intake behavior is now covered by:
  - `.\.venv\Scripts\python -m pytest -q tests/test_main_window.py`
  - `.\.venv\Scripts\python -m pytest -q tests/test_paths_and_db.py tests/test_queue.py tests/test_scraper.py tests/test_downloader.py`
- The current Processing crop/title changes were verified with:
  - `.\.venv\Scripts\python -m pytest -q tests/test_processing.py` -> `9 passed`
  - `.\.venv\Scripts\python -m pytest -q tests/test_main_window.py -k processing` -> `12 passed, 47 deselected`
  - `.\.venv\Scripts\python -m pytest -q tests/test_main_window.py` -> `59 passed`
- The `main_window.py` maintainability pass was verified with:
  - `.\.venv\Scripts\python -m pytest -q tests/test_main_window.py`
  - `.\.venv\Scripts\python -m pytest -q tests/test_queue.py tests/test_paths_and_db.py`
- The sidebar rail refresh was verified with:
  - `.\.venv\Scripts\python.exe -m pytest tests\test_main_window.py::test_workspace_is_blocked_without_current_account tests\test_main_window.py::test_sidebar_brand_is_display_only tests\test_main_window.py::test_sidebar_selected_state_and_compact_width tests\test_main_window.py::test_sidebar_toggle_and_compact_library_behavior tests\test_main_window.py::test_account_panel_does_not_overlap_sidebar_or_workspace tests\test_main_window.py::test_accounts_page_keeps_account_manager_visible -v` -> `6 passed`
  - timed local app startup smoke via `.venv\Scripts\python.exe` with real `MainWindow()` creation -> `main-window-smoke-ok`
- Sidebar-specific regression coverage now includes:
  - passive brand label behavior
  - compact rail width and explicit selected-state behavior
  - account-panel vs sidebar/workspace non-overlap geometry

## Best Next Milestone

Make the first Processing output trustworthy for repeated daily use.

The scrape/download foundation is already in place. The next highest-value work is validating Processing against real videos, tightening auto-crop/title behavior, and keeping AI-assisted generation useful without turning it into a bulk-cost workflow.

## Next Actions

1. Manually validate the latest Processing output on a few real videos, especially Shorts with blank/title bars
2. Keep tuning title sizing and crop precision against real exports
3. Continue improving caption draft quality as editable copy only
4. Later add a manual direct-link intake path for a single YouTube / Shorts URL without broadening the scraping scope

## Commands Used Or Recommended For Verification

- `Get-Content AGENTS.md`
- `Get-Content PLAN.md`
- `Get-Content STATUS.md`
- `Get-Content PROMPT.md`
- `Get-Content src/nicheflow_studio/app/main_window.py`
- `Get-Content src/nicheflow_studio/core/paths.py`
- `Get-Content tests/test_paths_and_db.py`
- `Get-ChildItem -Recurse -File -Exclude *.pyc | Where-Object { $_.FullName -notlike '*\.venv\*' -and $_.FullName -notlike '*\.git\*' } | Select-String -Pattern 'PyInstaller|pyinstaller|\.spec'`
- `.\.venv\Scripts\python.exe --version`
- `.\.venv\Scripts\python -m pytest -q tests/test_paths_and_db.py`
- `.\.venv\Scripts\python -m pytest -q tests/test_processing.py`

## Open Decisions / Blockers

- Should `yt-dlp` be bundled with the packaged app, installed separately, or pinned and updated another way?
- Which Windows versions must the packaged MVP support?
- Are the current candidate and download review semantics now sufficient, or do they still need more consolidation later?
- How much candidate metadata is actually needed next: channel name/date may be enough, or users may want richer fields before wider scraping
- Should future versions show a cross-account duplicate warning without blocking intake?
- Should backup restore eventually add a file picker and stronger overwrite confirmation flow?
- When the manual direct-link intake path is added, should it land in Scraping, Downloads, or as a small shared ingest action?

## Resume Here

Next highest-value task:
Manually validate the updated Processing crop/title behavior against real exported videos and keep tightening the output quality.

## Session Handoff: 2026-04-16 Sidebar Rail Refresh

What changed in this session:

- refreshed the compact left sidebar rail in `src/nicheflow_studio/app/main_window.py`
- replaced the button-like `NF` mark with a passive `NicheFlow` label
- increased compact rail width from the too-tight old size to reduce icon clipping
- added explicit selected-state styling for active navigation
- anchored the bottom account-toggle control more cleanly in the rail
- added regression tests in `tests/test_main_window.py` for:
  - passive brand label behavior
  - compact rail width and selected-state behavior
  - account panel / sidebar / workspace non-overlap geometry

Important context for the next chat:

- `src/nicheflow_studio/app/main_window.py` already had large preexisting uncommitted changes relative to `HEAD` before this sidebar work
- because of that dirty same-file baseline, this sidebar refresh was intentionally not isolated into a clean feature-only commit
- the workspace branch during this session was `codex/groq-two-step-processing`
- the packaged Windows `.exe` was not tested in this session; only Python-run launch + targeted sidebar tests were verified

Best immediate continuation if resuming this thread:

1. verify whether the packaged `.exe` in `dist/` launches with the refreshed sidebar
2. visually confirm the sidebar appearance in the packaged build
3. if packaging is fine, return to the broader Processing validation work listed above
