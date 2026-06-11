# WO-12 Packaged Webview Smoke Results

Validated on Windows 11 on 2026-06-11.

## Checklist

| Step | Result | Evidence |
| --- | --- | --- |
| `scripts/build_webview.ps1` clean build | Pass | Frontend and PyInstaller build completed; the package contains `frontend/dist/index.html` and the standalone `yt-dlp.exe` sidecar. |
| Fresh packaged launch | Pass | `scripts/run_fresh_packaged.ps1 -ResetData` launched one responsive `NicheFlowProcessing` window using `data/packaged-webview-smoke`. |
| Startup backup | Pass | Fresh launch created one zip containing `nicheflow.db`. |
| Accounts screen loads | Pass | Local window screenshot showed the fresh Accounts empty state. |
| Core import through dry-run auto-publish with Processing closed | Owner manual | Skipped because it requires real account data and a live publishing workflow. |
| Close and relaunch persists state | Pass | Relaunch reused the same smoke data directory and database. |
| No duplicate backup within 24 hours | Pass | Backup count remained `1` after relaunch. |
| yt-dlp packaged staleness | Pass | Package includes the official standalone sidecar; startup invokes its `-U` check in a daemon thread, and packaged URL downloads invoke the sidecar by subprocess. |

## Follow-ups And Workarounds

- `scripts/run_fresh_packaged.ps1` previously targeted and installed the legacy
  PyQt `NicheFlowStudio` package. It now targets the webview
  `NicheFlowProcessing` package and uses an isolated repo-local smoke data
  directory. No local install refresh is part of the webview smoke.
- Chromium-rendered pywebview text is not exposed through Windows UI Automation
  in this environment. Accounts-screen validation used a local window
  screenshot instead.
- The fresh packaged smoke does not exercise a real Instagram session, live
  publishing, or the complete import-to-auto-publish flow. The owner must run
  that checklist step with an appropriate test account.
- Packaged YouTube and Instagram URL downloads use the updater-capable sidecar.
  The scraper metadata fallback paths still use the frozen Python `yt_dlp` API;
  moving those to the CLI would require a broader scraper contract change and
  should be handled separately if metadata staleness becomes a packaged issue.
