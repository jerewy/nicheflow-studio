# NicheFlow Studio - Current Operational Scope (Windows-first)

Last updated: 2026-06-06

This document describes the product loop that already exists and must remain working while the UI is migrated incrementally.

## In Scope

- Windows-first desktop application
- Current UI: PyQt6
- Target UI: pywebview + Vite/React/TypeScript/Tailwind/shadcn, migrated one workflow at a time
- Existing Python backend remains in place
- Local runtime data under `data/`:
  - SQLite DB: `data/nicheflow.db`
  - downloads, processed Reels, logs, browser profiles, temp files, and caches
- Instagram-first source and publishing workflow:
  - Apify-backed source intake
  - public Reel/post download and local MP4 import
  - shared niche pools, global media deduplication, assignments, and multi-account distribution
  - smart draft generation with editable title/caption options
  - Instagram-ready rendering and export
  - publish now, scheduling, batch publishing, and automatic due-post checks
  - account session health and publish safeguards
- YouTube/YouTube Shorts via `yt-dlp` as a secondary intake path
- Background workers for scraping, generation, rendering, and publishing
- Packaged Windows delivery

## Active Milestone

Migrate the Processing workflow as the first pywebview/React vertical slice while preserving the current PyQt workflow until the replacement is packaged and validated.

See `UI_MIGRATION_PLAN.md` for the migration contract and `SOURCING_POOLING_PLAN.md` for the current sourcing/distribution architecture.

## Out of Scope For Now

- TikTok ingestion or publishing
- Official Instagram Graph API integration
- Evasion or stealth systems intended to bypass platform safeguards
- Reposting content without rights
- Cloud sync and multi-device web access
- Broad analytics dashboards
- Speculative ML pipelines such as embeddings, virality scoring, or drift detection
- Rewriting the Python backend, replacing SQLite, or moving media processing into the frontend

## Non-Goals

- Cross-platform packaging during the current migration
- Migrating every screen before the Processing vertical slice is packaged and validated
