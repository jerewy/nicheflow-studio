# NicheFlow Capture Extension

The private Chrome/Edge extension sends the active Instagram Reel directly to a
NicheFlow shared pool. It extracts metadata through the existing Apify path,
creates an accepted pending pool item, and deduplicates by URL/shortcode.

## Install

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked** and select:

   `browser-extension/nicheflow-capture`

4. Copy the extension ID shown by the browser.
5. Register the local Native Messaging host:

   ```powershell
   .\scripts\install_capture_extension.ps1 -ExtensionId <extension-id>
   ```

The installer builds `dist/NicheFlowCaptureHost.exe`, writes the local host
configuration under `%LOCALAPPDATA%\NicheFlow Studio`, and registers it for both
Chrome and Edge.

## Use

Open an Instagram Reel and click the NicheFlow extension icon. Choose a pool and
click **Queue Current**. Repeat on additional Reels, then click **Process Queue**.
NicheFlow sends the queued URLs to Apify in one batch and processes it in the
background, so the compact popup can be closed immediately. The badge shows the
queued count and the popup shows pool size, estimated Apify spend, and the last
batch result.

The extension badge and desktop notification report whether the Reel was added,
was already pooled, or failed metadata extraction.
