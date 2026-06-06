# NicheFlow Studio Status

Last updated: 2026-06-06

## Current Focus

Begin the incremental UI-layer migration with a Processing-first pywebview/React vertical slice.

Keep the existing Python backend and operational PyQt workflow working while extracting only the services and background-job boundaries required by the new Processing screen. Package and validate the first replacement slice before migrating another screen.

## Current Product Reality

- Windows-first desktop application
- Current UI: PyQt6
- Target UI: pywebview + Vite/React/TypeScript/Tailwind/shadcn
- Backend: Python, SQLite, FFmpeg, yt-dlp, Apify integrations, AI providers, and Playwright
- Primary target: Instagram sourcing, processing, multi-account distribution, scheduling, and publishing
- Secondary intake path: YouTube/YouTube Shorts through `yt-dlp`

The earlier manual-publishing and Processing-only status is stale. The repository has already shipped beyond those gates.

## Implemented Capabilities

### Source, Library, And Distribution

- Account management, account health, and per-account configuration
- Apify-backed Instagram source intake and single-URL import
- Public Instagram media download and local MP4 import
- Secondary YouTube/Shorts intake via `yt-dlp`
- Download queue, retry/failure handling, review actions, and local library
- Global media registration/deduplication
- Shared niche pools, pool intake/pruning tools, assignments, and balanced distribution
- Pool/admin scripts and in-app pooling/distribution controls

### Processing

- Source preview and processed-output preview
- Transcript and visual-context support
- Smart title/caption generation with account/profile/style rules
- Three editable draft option cards, recommendations, notes, and style metadata
- Copy Chat Prompt and Paste Draft fallback workflow
- Cinema Bold keyword markup support
- Automatic crop/title-band handling
- Title overlay rendering, cover/thumbnail selection, and Reel export
- Export-to-publish-queue handoff

### Publishing

- Publish Queue and Publishing Dashboard
- Publish now
- Scheduled publishing and account posting slots
- Batch publishing and Publish All Due
- Automatic due-post polling
- Playwright Instagram Reel publisher
- Safe mode, daily caps, cooldowns, checkpoint handling, and account session health
- Posted URL/state persistence and duplicate queue-row collapse

### Runtime And Packaging

- SQLite compatibility upgrades and local runtime path policy
- Repo-local development data and packaged per-user data behavior
- PyInstaller build flow and packaged smoke-test scripts
- Broad automated coverage across processing, UI flows, pooling, assignments, scheduling, publishing, and data helpers

## Active Architectural Decision

Keep the backend stack. Replace only the UI layer incrementally.

- First slice: Processing
- Long-running AI, scrape, download, FFmpeg, file, and Playwright work runs as UI-independent background jobs
- Bridge calls return quickly with structured data or job IDs
- Poll job progress first; add pushed events only if polling proves insufficient
- Keep PyQt paths until replacement parity is packaged and validated

See `docs/UI_MIGRATION_PLAN.md` for the authoritative migration contract.

## Next Actions

1. Create the minimal pywebview shell and Vite/React/TypeScript frontend.
2. Define the smallest UI-independent background-job contract.
3. Extract the minimum Processing application services currently embedded in `main_window.py`.
4. Implement the React Processing workspace with direct structured draft generation/revision.
5. Connect saved selection, export progress, scheduling, and publish actions.
6. Build and smoke-test the packaged Windows Processing slice.
7. Only then choose the next screen to migrate.

## Known Risks And Constraints

- `src/nicheflow_studio/app/main_window.py` remains a large mixed UI/workflow module; extraction must stay scoped to the active slice.
- React improves layout, state handling, and maintainability but does not make FFmpeg, AI, Playwright, database, or file operations faster by itself.
- Background work must not block pywebview bridge calls or hold SQLite transactions while waiting on external operations.
- The packaged pywebview/React asset path and bridge behavior must be validated early.
- The current git worktree contains substantial ongoing changes; migration work must preserve them.
- Platform, originality, rights, and account-footprint risks documented in `docs/SOURCING_POOLING_PLAN.md` remain accepted/current.

## Deferred

- Rewriting the Python backend or replacing SQLite
- Local HTTP API, Next.js, or Electron for the first desktop migration
- Migrating all screens before the first packaged vertical slice
- Multi-device/cloud-hosted web app
- TikTok, cloud sync, broad analytics, and speculative ML features

## Resume Here

Start with the Processing-first implementation sequence in `docs/UI_MIGRATION_PLAN.md`. Do not resume from the old manual Publish Queue or Processing-only milestone descriptions.
