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

## Obsidian Second Brain

- This repo maps to the `nicheflow` project in `C:\Users\ASUS\.codex\memories\workspace-project-map.json`.
- At the start of substantive work in this repo, run:
  - `node C:\Users\ASUS\.codex\scripts\codex-second-brain.mjs bootstrap-session --workspace-path "C:\dev\nicheflow-studio" --text "<current task>"`
- Read the returned shared-core files first, then the returned Nicheflow project files.
- For code changes, bug investigations, multi-step tasks, or work that should survive context compaction, create a task/session note with:
  - `node C:\Users\ASUS\.codex\scripts\codex-second-brain.mjs start-task-session --title "<task title>" --goal "<goal>" --user-ask "<original ask>" --primary-project "nicheflow" --working-directory "C:\dev\nicheflow-studio"`
- During longer tasks, append concise progress updates to the active session note.
- At checkpoints or task completion, close the session with a concise summary and next prompt so `Tasks/`, `Sessions/`, `Prompts/`, and `Indexes/active-context.md` stay current.
- Keep `Daily/YYYY-MM-DD.md` human-written only; do not append daily notes automatically.

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
