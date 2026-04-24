# NicheFlow Studio Plan

Last updated: 2026-04-05
Status: Active execution plan
Current milestone: Finish and harden the first Processing slice on top of the working scrape/download intake flow.

## 1. What This Project Should Be Right Now

NicheFlow Studio should ship first as a small, reliable Windows desktop app for a multi-account clipping workflow.

The long-term product is not just a downloader. It is a system that helps a user:

1. choose and manage a content account/profile
2. acquire content for that account
3. build and manage a local library of candidate clips
4. prepare the workflow for later processing and uploading

For the current MVP, only the first reliable slice needs to be finished:

- account management
- account selection
- YouTube / YouTube Shorts acquisition
- download history and local file tracking
- packaged Windows delivery

Current strategy:

- Keep the account -> scrape -> download -> process flow coherent.
- Improve Processing without reopening broad platform scope.
- Keep implementation smaller than the vision.
- Package it early so the MVP is real outside the dev environment.
- Keep hosted/local AI usage confined to chosen videos in Processing.

## 2. Product Direction

### Long-Term Product Vision

A multi-account Auto Clipper desktop app that helps users manage niche-specific accounts, discover or ingest suitable content, prevent duplicate work, process raw clips, and prepare or publish them to target platforms.

### Current MVP

The MVP is the first operational slice of that system:

- Windows-only desktop app using PyQt6
- account CRUD and account selection
- niche/account-aware workspace gating
- local runtime data with override support for dev/testing
- SQLite database for accounts and download history
- YouTube / YouTube Shorts ingestion via `yt-dlp`
- download queue with background workers
- local library/history with status, retry, remove, open, and review actions
- enough packaging work to run the app outside the dev environment

### Explicitly Deferred

These are part of the broader vision, but should not drive current implementation:

- TikTok and Instagram ingestion
- automated smart scraping/discovery across multiple sources
- uploader automation
- broad processing automation beyond the current title/crop workflow
- AI caption generation, embeddings, niche scoring, virality scoring, or drift detection
- analytics dashboards
- cloud sync
- stealth / anti-detection systems

## 3. Core User Flow

The product flow should stay stable even while the MVP is narrow.

### Flow A: Account-Centered Workflow

1. User creates, edits, deletes, and selects an account/profile.
2. Each account/profile represents a niche or content direction.
3. User acquires content for the selected account.
4. Acquired content is stored in the local library/history.
5. User reviews chosen items and moves selected videos into Processing.
6. Processing generates title/caption drafts, auto-crops when needed, and exports a first processed output.

### Flow B: Future Full Auto Clipper Flow

This is the intended future direction, not the MVP scope:

1. choose account
2. discover/scrape suitable content for that account
3. avoid duplicates and low-value content
4. process the raw clip
5. prepare title/caption/format
6. upload to a target platform

The MVP must not break this future flow. It should be the foundation for it.

## 4. Execution Principles

1. Protect the real product flow.
   The current implementation may be smaller than the vision, but every major decision should still fit the account -> acquire -> library -> later process/upload flow.

2. Prefer reliable workflow over broad feature count.
   One working account-based acquisition loop is more valuable than many incomplete systems.

3. Keep the MVP smaller than the ambition.
   The vision is bigger than the current milestone. That is intentional.

4. Package earlier than feels comfortable.
   The MVP is not truly real until it runs outside the dev environment.

5. Avoid speculative architecture.
   Add abstractions only when the second real use case arrives.

6. Keep repo guidance tied to actual progress.
   `PLAN.md` tracks roadmap direction.
   `STATUS.md` tracks current reality and blockers.

## 5. Current Progress Snapshot

### Already Implemented

- [x] Python package entrypoint via `python -m nicheflow_studio`
- [x] PyQt6 desktop app bootstrap
- [x] local runtime path setup with `NICHEFLOW_DATA_DIR` override support
- [x] packaged Windows runtime path behavior
- [x] logging setup
- [x] SQLite initialization via SQLAlchemy
- [x] account model
- [x] download item model
- [x] basic schema compatibility upgrades for existing databases
- [x] `yt-dlp` downloader wrapper for YouTube URLs
- [x] background download queue with threaded execution
- [x] failure capture with sanitized error messages
- [x] main window for account selection, queue table, and item details
- [x] retry flow
- [x] remove-from-history flow
- [x] file open flow
- [x] dev and run PowerShell scripts
- [x] minimal PyInstaller-based Windows packaging flow
- [x] first real packaged smoke test
- [x] first real packaged download with persisted runtime data after restart
- [x] automated tests for queue behavior, DB/path setup, and major UI flows
- [x] source-based scraping with `Source` and `ScrapeRun` models
- [x] background scrape worker with live progress updates
- [x] sidebar-based module shell with separate Scraping / Downloads / Processing / Uploads / Accounts destinations
- [x] candidate-state filter and color-coded candidate states in the scraping UI
- [x] regression fix for unassigned download visibility
- [x] regression fix for resetting linked scrape candidates when a download row is removed
- [x] tabbed scraping workspace for Sources / Candidates / Runs
- [x] source filter/sort controls and inline enabled dropdowns
- [x] source URL normalization from channel/profile subpages to root URLs
- [x] source-level scrape progress bar
- [x] clearer candidate review labels and reversible ignore flow
- [x] account-scoped duplicate handling during scrape intake

### Needs Hardening

- [x] improve URL validation before queueing
- [x] improve pre-submit failure handling for bad input
- [x] improve downloader failure messaging for common `yt-dlp` failures that still reach the queue
- [x] add minimum useful duplicate protection in the submit path
- [x] verify packaged `Open Video` / `Open Folder` behavior on Windows
- [x] accept current `Open Folder` behavior for MVP
- [x] document packaged update/upgrade expectations
- [x] narrow one obvious maintainability seam in `main_window.py` without broad refactoring
- [x] move scraping work off the UI thread
- [x] show scrape progress/status while scraping runs
- [x] separate account management into its own page destination

### Not Started

- [x] richer metadata visibility for existing library items
- [x] stronger duplicate protection beyond source URL/history awareness
- [x] import/export or backup support
- [x] batch-safe review actions in Downloads
- [x] first Processing slice with transcript-driven draft generation
- [x] chosen-video-only smart generation in Processing
- [x] Processing source preview and processed-output preview
- [x] title-only processed export with automatic crop suggestion
- [x] dark-title-band detection for crop suggestions
- [ ] uploader integration
- [x] scraping/intake `v0`

## 6. Current Milestones

### Milestone 1: Finish Processing V1

Goal: Make the selected-video Processing flow strong enough for daily use.

Tasks:

- [x] keep LLM usage confined to Processing for chosen videos
- [x] generate transcript/title/caption drafts for the selected item
- [x] show original and processed preview states in Processing
- [x] auto-crop only when the video actually needs it
- [x] trim repeated dark title bars and blank bands when present
- [x] render only the title into the processed output
- [ ] continue tuning title size, title styling, and crop precision against real videos
- [ ] continue tuning caption draft quality as editable copy, not baked output
- [ ] manually validate a few real exported outputs end to end

Definition of done:

- A selected downloaded video can move through Processing and produce a usable first output with a sensible crop and a readable title overlay.

### Milestone 2: Harden Account-Based Acquisition

Goal: Make the acquisition loop trustworthy for repeated daily use.

Tasks:

- [x] add lightweight URL validation and clearer pre-submit errors
- [x] harden downloader failure messaging for common `yt-dlp` failures
- [ ] manually verify retry, open, remove, selection persistence, and refresh behavior on Windows
- [x] manually verify packaged `Open Video` and `Open Folder` shell behavior on a real downloaded file
- [x] add one smoke-test checklist for a successful Shorts download and a known failure case
- [x] add minimum useful duplicate protection rules
- [x] narrow obvious maintainability pressure in `main_window.py` without broad refactoring

Definition of done:

- A user can manage accounts, choose the correct account, submit valid YouTube/Shorts links, avoid obvious duplicate acquisition mistakes, recover cleanly from common failures, and trust the local library state.

### Milestone 3: Improve Library Quality Without Breaking Scope

Goal: Make the local library more useful while staying inside the acquisition foundation.

Tasks:

- [x] add richer metadata visibility where already available
- [x] clarify review workflow semantics across scraped candidates
- [x] align download review language more closely with the candidate review language
- [x] add batch-safe actions only if repeated friction appears
- [ ] improve small workflow pain points only when they are concrete and recurring

Definition of done:

- The app is pleasant enough to manage a small niche-specific content library every day.

### Milestone 4: Scraping / Intake V0

Start after Milestones 1-2 are sufficiently stable.

Goal: Add the smallest metadata-first source intake flow that supports the future auto-clipper direction without taking on full scraping complexity.

Tasks:

- [x] choose one supported source input for `v0`
- [x] ingest candidate YouTube items for the selected account without auto-download
- [x] persist candidate metadata separately from download history
- [x] avoid re-adding obvious duplicates using existing stable identifiers where possible
- [x] keep ranking, uploader automation, and non-YouTube sources out of scope

Definition of done:

- A user can point the app at a supported YouTube source, ingest candidate items for one selected account, and queue a selected candidate into the existing download flow.

### Milestone 4A: Scraping UX Hardening

Goal: Make the first scraping slice usable for repeated daily intake work.

Tasks:

- [x] move scraping off the UI thread
- [x] show progress/status updates while scraping runs
- [x] separate scraping and downloads into clearer module pages
- [x] give account management its own page destination
- [x] improve candidate-state visibility and filtering
- [x] make source management clearer and more structured
- [x] add a visible source-level progress bar
- [ ] manually validate intake with multiple real YouTube sources

Definition of done:

- A user can manage sources, run scrapes, review candidates, and queue selected items without the app feeling confusing or frozen.

### Milestone 5: Expand Carefully

Start only after Milestones 1-4 are complete.

Possible future work:

- [ ] smart scraping/discovery for selected accounts
- [ ] direct manual intake from a single YouTube / Shorts URL such as `https://www.youtube.com/shorts/...`
- [ ] stronger duplicate/content-fit rules
- [ ] scheduling
- [ ] uploaders
- [ ] analytics

## 7. Immediate Priority Backlog

Ordered next work:

1. Manually validate the new crop/title behavior against a few real exported outputs
2. Continue tuning title overlay sizing and styling against real processed videos
3. Continue improving caption draft quality for editing, not baked output
4. Add a later manual direct-link intake path for a single YouTube / Shorts URL when the current Processing slice is stable

## 8. Open Questions / Decision Points

- Should `yt-dlp` be bundled, installed separately, or version-pinned in a controlled way?
- Should cross-account duplicate handling become a warning-only signal later instead of a hard block?
- Which Windows versions must be supported for MVP?
- What account fields are truly needed now versus later?

## 9. Architecture Guardrails

- Keep `queue.py` responsible for job orchestration only.
- Keep `downloader/youtube.py` responsible for `yt-dlp` interaction only.
- Keep DB migration logic minimal until schema change frequency justifies something heavier.
- Keep account management simple unless a concrete second workflow appears.
- Split `main_window.py` only when a concrete seam appears:
  - account management panel
  - library table rendering
  - detail panel actions
- Do not add platform abstraction layers until a second real downloader exists.
- Keep hosted AI usage inside Processing for chosen videos only.
- Do not design the uploader architecture in advance of actual MVP pressure.

## 10. Risks

### High Risk

- `yt-dlp` works today but drifts again outside the dev environment
- packaged Windows shell handoff may still differ from source-run behavior
- current `Open Folder` wording may over-promise compared with actual behavior if it only opens the containing folder
- UI logic keeps accumulating in `main_window.py`

### Medium Risk

- ad hoc schema upgrades become messy after more DB changes
- account credential storage becomes a real concern if it grows beyond notes/metadata
- current global duplicate suppression is too strict for multi-account workflows where overlapping accounts should still review the same source video independently
- the source-management UI is still functional rather than fully clear, so scaling from one source to many may feel clumsy

## 11. Success Criteria

### MVP Success

- [ ] Windows user can run the app without manual code changes
- [ ] user can create, edit, delete, and select an account/profile
- [ ] YouTube and YouTube Shorts downloads succeed reliably for the selected account
- [ ] failures produce readable messages
- [ ] download history is useful and stable
- [ ] packaged runtime behavior is predictable
- [ ] the app clearly feels like the first slice of a multi-account Auto Clipper workflow

### Post-MVP Success

- [ ] packaged releases are repeatable
- [ ] library management friction is low
- [ ] minimum duplicate protection is working
- [ ] expanding to smart scraping, processing, or uploaders does not require major rewrites

## 12. What Not To Do Yet

Do not spend time on these until the current Processing and acquisition hardening work is done:

- embeddings / niche scoring
- caption removal automation
- advanced clip editing
- TikTok or Instagram upload automation
- stealth / anti-detection work
- complex scheduler logic
- cloud sync

## 13. Recommended Next Step

Manually validate the updated Processing crop/title behavior against real exported videos, then keep tightening output quality before adding the manual direct-link intake path.
