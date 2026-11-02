# NicheFlow Studio — AGENTS.md

## Scope

- Applies to the entire `nicheflow-studio` repository.

## Goal

- Ship a reliable Windows-first multi-account Instagram clipping and publishing workflow.
- Preserve the working Python backend while incrementally replacing the oversized PyQt6 UI.

## Product Boundaries

- Current delivery target is Windows desktop.
- Instagram is the primary sourcing/publishing target; YouTube/`yt-dlp` remains a secondary intake path.
- Existing Instagram publishing, scheduling, shared-pool, and distribution behavior is in scope and must not regress.
- The active UI direction is pywebview + Vite/React/TypeScript/Tailwind/shadcn, migrated one workflow at a time.
- Keep Python, SQLite, FFmpeg, Playwright, scraping, processing, pooling, scheduling, and publishing as the backend.
- Defer TikTok, cloud sync, broad analytics, and speculative ML features.

## Working Style

- Prefer small, incremental changes with runnable checkpoints.
- Fix the reported problem at the root cause without expanding scope unless necessary.
- Prefer matching existing repo patterns over introducing new abstractions.
- Avoid broad refactors unless they clearly unblock the current MVP.

## Architecture Preferences

- Keep core logic OS-agnostic where easy (`pathlib`, isolated filesystem logic), even though packaging is Windows-only for MVP.
- Keep downloader-specific logic isolated from UI and database logic.
- Add abstractions only when a second real use case exists.
- Move workflow behavior out of PyQt widgets into plain Python services when touching it.
- Long-running AI, FFmpeg, file, scrape, and Playwright operations must run as background jobs and expose structured progress.
- Keep pywebview bridge calls thin and fast; pass small JSON payloads and file paths, not media bytes.

## Local Data

- All runtime data goes under `data/` and should remain gitignored.
- This includes downloads, SQLite DB, logs, temp files, and caches.

## Dependency Rules

- Avoid new dependencies unless clearly justified.
- If adding a dependency, explain what it does, why it is needed, and why the standard library or current stack is not enough.

## Verification

- After changes, run the smallest relevant verification available.
- Prefer proving behavior with tests, targeted manual verification, or both.
- For bug fixes, verify the issue before and after when possible.
- Do not claim something works without evidence.

## Packaging Priority

- Packaging is part of the MVP, not a post-MVP luxury.
- Prefer the smallest reliable packaging path first.
- Optimize for a packaged build that can run outside the dev environment.

## What Not To Do Yet

- Do not rewrite the Python backend, replace SQLite, or introduce a local HTTP server for the first UI migration slice.
- Do not migrate every screen before the Processing vertical slice is packaged and validated.
- Do not add major new PyQt UI surfaces unless needed to preserve the current workflow during migration.
- Do not build TikTok, cloud sync, broad analytics, or speculative ML features yet.
