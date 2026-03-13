# NicheFlow Studio — Agent Notes

## Scope
- This file applies to the entire `nicheflow-studio` repository.

## Goal (MVP)
- Windows-only desktop MVP first.
- Support YouTube Shorts/YouTube URL ingestion via `yt-dlp` (Phase 1), then expand platforms later.

## Conventions
- Prefer small, incremental changes and runnable checkpoints.
- Avoid new dependencies unless justified and approved by the user.
- Keep core logic OS-agnostic where easy (use `pathlib`), but packaging can stay Windows-only for MVP.

## Local Data
- All runtime data goes under `data/` (ignored by git): downloads, SQLite DB, logs, caches.

