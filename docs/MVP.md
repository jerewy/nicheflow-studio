# NicheFlow Studio - MVP Scope (Windows-first)

This MVP is designed to be runnable and useful quickly, while keeping the codebase structured for later growth.

## In Scope (MVP)

- Windows-only desktop app (PyQt6)
- Local runtime directory: `data/` (ignored by git)
  - SQLite DB: `data/nicheflow.db`
  - Downloads: `data/downloads/`
  - Processed Reels: `data/processed/`
  - Logs: `data/logs/`
- Instagram-first publishing workflow:
  - prepare Instagram-ready vertical Reels
  - copy captions
  - open the exported Reel
  - mark posts as posted
  - manually track posted URL and basic metrics
- YouTube ingestion via `yt-dlp` as a source pipeline, including YouTube Shorts URLs:
  - paste URL -> download -> record in DB -> show status in UI
- Instagram source intake via Apify:
  - save Instagram Reel/profile/hashtag references as candidate ideas
  - fetch public metadata through `apify/instagram-scraper`
  - review candidates alongside other sources
  - keep logged-in Instagram scraping out of the normal app flow
- Processing workflow:
  - black-canvas/no-blur vertical template
  - clean title rendering
  - editable caption metadata
  - processed output can be added to the Publish Queue

## Out of Scope (Later)

- Official Instagram Graph API intake and publishing automation
- TikTok ingestion
- "Stealth" automation such as fingerprinting, human-sim input, or logged-in scraping
- Reposting Instagram content without rights
- ML verification pipeline such as embeddings, drift detection, or Whisper
- Analytics dashboards beyond manual metric tracking

## Non-Goals

- Cross-platform packaging in MVP; keep code mostly portable, but ship Windows first
- Perfect UI/UX polish; functional and reliable comes first
