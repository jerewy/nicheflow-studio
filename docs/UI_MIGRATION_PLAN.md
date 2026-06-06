# NicheFlow Studio UI Migration Plan

Last updated: 2026-06-06
Status: Approved direction; implementation not started

## Decision

Begin an incremental UI-layer migration now. Keep the working Python backend and replace PyQt6 one workflow at a time with a pywebview-hosted React frontend.

The first vertical slice is Processing because it contains the clearest daily-use friction, the largest interactive state surface, and the desired direct AI revision workflow.

## Why The Previous Gate Is Retired

The May 2026 plan delayed UI migration until a manual Instagram Publish Queue and source-intake MVP shipped. The repository has since moved beyond that gate:

- Instagram publish-now, scheduling, batch publishing, automatic due-post checks, and account session health exist.
- Apify source intake, shared niche pools, global media deduplication, assignments, and distribution exist.
- Processing already supports smart drafts, editable options, export, and queue/publish handoff.
- `src/nicheflow_studio/app/main_window.py` has grown into a large mixed UI/workflow module and is now a delivery constraint.

## Target Stack

- Desktop shell: pywebview
- Frontend: Vite + React + TypeScript + Tailwind + shadcn/ui
- Backend: existing Python modules and services
- Persistence: SQLite and local files
- Media and automation: FFmpeg, yt-dlp, Apify integrations, Groq/AI providers, and Playwright
- Packaging: Vite static build bundled with the Python application through PyInstaller

Do not introduce Next.js, Electron, or a local HTTP server for the first desktop migration slice.

## Execution Rules

- Migrate one end-to-end workflow at a time.
- Extract plain Python application services from PyQt handlers only as needed by the active slice.
- Long-running AI, scrape, download, FFmpeg, file, and Playwright operations run as background jobs.
- Bridge calls start work and return quickly with structured data or a job ID.
- The frontend receives progress by simple polling first; add pushed events only if polling proves insufficient.
- Pass file paths and small JSON payloads through the bridge, never media bytes.
- Keep the existing PyQt workflow available until the replacement slice is packaged and validated.
- Avoid major new PyQt UI work during migration.

## Background Job Contract

Jobs should expose a UI-independent shape such as:

```json
{
  "id": "job-123",
  "type": "export",
  "status": "running",
  "progress": 62,
  "message": "Rendering title overlay",
  "result": null,
  "error": null
}
```

Initial job types should cover draft generation/revision, export, scraping/download, and publish. Job orchestration must not hold SQLite transactions while waiting on network, FFmpeg, or Playwright work.

## Processing-First Vertical Slice

1. Create a minimal pywebview shell that loads a React Processing screen.
2. Add thin Python services/bridge methods for:
   - loading selected video, account, template, and saved draft context
   - generating structured draft options
   - revising one option from a user instruction
   - saving and selecting a draft
   - starting export and reading progress
   - adding/updating a publish job
   - publishing now or scheduling
3. Build the React Processing workspace:
   - video/source preview
   - editable title/caption option cards
   - Cinema Bold keyword markup preview
   - direct revision instruction input
   - selected-draft state
   - export progress and result
   - schedule/publish actions
4. Keep Copy Chat Prompt and Paste Draft temporarily as fallback paths.
5. Build and smoke-test a packaged Windows artifact before migrating another screen.

## Definition Of Done For The First Slice

- A user can select a real local video, generate or revise structured options inside NicheFlow, select/edit one option, export it, and schedule or publish it without copying text between chat and the app.
- Long-running work does not freeze the React UI.
- The packaged Windows build completes the same Processing workflow.
- The old PyQt Processing path remains available until the new path passes the packaged smoke test.

## Later Migration Order

1. Processing
2. Publishing Dashboard / Publish Queue
3. Pooling and distribution
4. Downloads and source intake
5. Accounts and settings
6. Retire PyQt6 and delete `main_window.py` only after all required workflows are replaced

## Deferred

- Multi-device/browser-hosted web app
- Cloud sync or remote workers
- Backend rewrite or database replacement
- Broad analytics and speculative ML features
