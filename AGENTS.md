# NicheFlow Studio — AGENTS.md

## Scope

- Applies to the entire `nicheflow-studio` repository.

## Goal

- Ship a small, reliable Windows-only desktop MVP first.
- Primary workflow: ingest YouTube / YouTube Shorts URLs, download them locally, and manage them through a usable desktop UI.

## Product Boundaries

- Current MVP is Windows-only.
- Support YouTube and YouTube Shorts first via `yt-dlp`.
- Defer multi-platform ingestion, ML scoring, analytics, cloud sync, and automation until the core local workflow is stable and packaged.

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

- Do not build for TikTok, Instagram, upload automation, analytics, cloud sync, or ML features until the YouTube core loop is stable and packaged.
- Do not introduce architecture for speculative future platforms.
