# NicheFlow Studio — MVP Scope (Windows-first)

This MVP is designed to be runnable and useful quickly, while keeping the codebase structured for later growth.

## In Scope (MVP)

- Windows-only desktop app (PyQt6)
- Local runtime directory: `data/` (ignored by git)
  - SQLite DB: `data/nicheflow.db`
  - Downloads: `data/downloads/`
  - Logs: `data/logs/`
- YouTube ingestion via `yt-dlp` (including YouTube Shorts URLs)
  - Paste URL → download → record in DB → show status in UI

## Out of Scope (Later)

- TikTok/Instagram ingestion
- “Stealth” automation (fingerprinting, human-sim input)
- ML verification pipeline (embeddings, drift detection, Whisper)
- Analytics dashboards

## Non-Goals

- Cross-platform packaging in MVP (we’ll keep code mostly portable, but ship Windows first)
- Perfect UI/UX polish (functional > pretty for now)

