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

## Why pywebview, Not Electron Or Tauri (Locked 2026-06-06)

The shell choice was re-examined against two stated goals: fast performance and fast UI development with a prebuilt component library (shadcn). The decision is to keep pywebview. Do not re-open this without concrete new evidence.

Rationale:

- The React component ecosystem (shadcn/ui, Tailwind, Radix, TanStack, etc.) is identical across pywebview, Tauri, and Electron — it lives in the frontend, not the shell. So "fast dev / good UI library" gives no reason to switch.
- On Windows, pywebview and Tauri both render through WebView2 (the same Chromium-based engine). pywebview is not slower at drawing UI, and it starts faster than Electron, which bundles its own Chromium.
- The real performance bottleneck is the Python backend (FFmpeg, Playwright, Apify, AI), which no shell choice speeds up. Perceived speed comes from the background-job + polling architecture and frontend caching/optimistic UI, not from the shell.
- pywebview is the only option with an in-process Python bridge. Tauri and Electron would force a separate Python process reached over local HTTP (a sidecar/subprocess to manage), adding latency and lifecycle complexity for a solo developer.
- Bundle size: pywebview and Tauri are tiny; Electron ships 120MB+.

Net: pywebview maximizes both stated goals (rich React ecosystem + small/simple ship) with the simplest backend integration. Fallback if a concrete WebView2 wall appears during the Processing slice is Tauri (keeps the small bundle), decided with evidence — not Electron.

## Execution Rules

- Migrate one end-to-end workflow at a time.
- Extract plain Python application services from PyQt handlers only as needed by the active slice.
- Long-running AI, scrape, download, FFmpeg, file, and Playwright operations run as background jobs.
- Bridge calls start work and return quickly with structured data or a job ID.
- The frontend receives progress by simple polling first; add pushed events only if polling proves insufficient.
- Pass file paths and small JSON payloads through the bridge, never media bytes.
- Keep the existing PyQt workflow available until the replacement slice is packaged and validated.
- Avoid major new PyQt UI work during migration.
- Use SQLite as the source of truth for generated draft revisions and the selected final draft.
- Let Codex and the React UI call the same plain Python draft-revision service.
- The React UI should poll/refetch the latest draft revision first; add pushed events only if polling proves insufficient.
- Never silently replace unsaved local edits when a newer database revision appears.

## Database-Backed Codex Draft Handoff

Replace the Copy Chat Prompt -> external generation -> Paste Draft workflow with a database-backed handoff.

When the user asks Codex to generate or revise drafts for the active NicheFlow item:

1. Codex reads the active Processing context through one repository CLI.
2. Codex inspects the local video and generates structured title/caption options.
3. Codex writes the structured result through the same CLI and shared Python service.
4. The service inserts a new versioned draft revision into SQLite.
5. The React Processing UI polls/refetches the latest revision and updates the affected option cards without restarting the app.

The CLI is an adapter only. Database rules and validation belong in a shared UI-independent service used by both the CLI and pywebview bridge.

Suggested interfaces:

```text
scripts/nicheflow_drafts.py
src/nicheflow_studio/services/draft_revisions.py
```

Suggested CLI commands:

```text
current
context --item-id <id>
save --item-id <id> --stdin
revise --item-id <id> --option <1-3> --stdin
apply --item-id <id> --option <1-3>
history --item-id <id>
```

The selected final draft remains on `DownloadItem` for export/publishing compatibility. A new versioned draft-revision table stores generated options, recommendations, styles, notes, source, and timestamps so revisions can be compared or recovered.

The UI may immediately apply a newer revision when there are no unsaved local edits. If the user is editing, show a newer-revision notice with explicit Review Update and Keep My Edits actions.

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

1. Add the versioned draft-revision model, shared service, and `scripts/nicheflow_drafts.py` CLI.
2. Prove Codex can read the active item context and save structured options into SQLite.
3. Create a minimal pywebview shell that loads a React Processing screen.
4. Add thin Python services/bridge methods for:
   - loading selected video, account, template, and saved draft context
   - polling/refetching the latest draft revision
   - generating structured draft options
   - revising one option from a user instruction
   - saving and selecting a draft
   - starting export and reading progress
   - adding/updating a publish job
   - publishing now or scheduling
5. Build the React Processing workspace:
   - video/source preview
   - editable title/caption option cards
   - Cinema Bold keyword markup preview
   - direct revision instruction input
   - selected-draft state
   - automatic revision refetch with dirty-edit protection
   - export progress and result
   - schedule/publish actions
6. Keep Copy Chat Prompt and Paste Draft temporarily as fallback paths.
7. Build and smoke-test a packaged Windows artifact before migrating another screen.

## Definition Of Done For The First Slice

- A user can select a real local video, generate or revise structured options inside NicheFlow, select/edit one option, export it, and schedule or publish it without copying text between chat and the app.
- Codex can save a structured draft revision through the repository CLI and the open React UI updates without restart or clipboard paste.
- Draft revisions are versioned in SQLite, while the selected final title/caption remains compatible with the existing export/publish path.
- Automatic refetch never silently overwrites unsaved local edits.
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
