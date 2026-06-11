# Backlog Execution Plan (prioritized work orders)

Planner: Claude session 2026-06-10/11. Executor: Codex (or any agent) — each
work order below is self-contained: what, why, where, how, verify.
Registry of raw findings stays in `PUBLISHING_SAFETY_BACKLOG.md`; this file is
the build order. Do the orders top to bottom unless a dependency says otherwise.

**Goal context:** multi-account Instagram network (history/movie niches),
monetized through audience growth. Engine: Apify sourcing → pool → distribute →
draft (Groq) → export → schedule → Playwright publish. Owner approves drafts
and exports manually (deliberate safety gate, keep it).
**Platform decision (2026-06-11):** React/webview is THE app. PyQt is
legacy-frozen — zero new features, retirement sweep scheduled below.

## Critical path (no-waste order, decided 2026-06-11)

Results = content quality × posting consistency × not getting banned.
Safety rails landed 2026-06-10; the remaining result-movers, in order:

1. **WO-2** hook upgrade — only item that directly raises engagement; free.
2. **WO-1** backend publish loop + **WO-11** backup — consistency: posts fire
   without babysitting, and the data that runs the business can't be lost.
3. **WO-12** packaged validation — locks 1+2 in as a real product.
4. **WO-14** white-canvas crop fix — first quality item after the hooks;
   every white-canvas export currently ships with a white halo.
5. **WO-3 + WO-4 + WO-13** auto-schedule on export, per-account cadence,
   missed-slot catch-up — removes the last per-post clicks, stops losing
   posts to skipped slots, enables the 4→5 ramp.
6. **WO-6** metrics feedback — makes quality compound over time.

Everything else (WO-5 side panel, WO-7 model swap, WO-8/9/10, discovery,
pipeline stages) waits until the five above are live — they are conveniences
or pay-offs that need the measurement loop first. WO-7 in particular: do NOT
pay for a model before WO-2's two-week engagement read says the prompt fix
wasn't enough.

---

## WO-1 — Move the auto-publish loop into the Python backend

**Priority: 1 (do first).** Unblocks everything hands-off; required by WO-3+.

- **What:** Scheduled posts currently fire only while the React Processing
  screen is mounted with the toggle on (`frontend/src/components/ProcessingScreen.tsx`,
  `AUTO_PUBLISH_INTERVAL_MS = 60000` drives `publish_due_jobs` through the
  bridge). Replace with a daemon thread in the Python backend that runs the
  same loop app-wide, regardless of which screen is open.
- **Why:** The owner's workflow is "export → it waits → it posts itself at the
  slot time". Today that silently breaks the moment the Processing screen
  unmounts. Every later automation stage (WO-3, pipeline stages) assumes a
  loop that always runs while the app is open.
- **Where:**
  - New: `src/nicheflow_studio/services/auto_publish_loop.py` (daemon thread:
    start/stop, 60 s tick, calls `publish_due_jobs()` when
    `auto_publish_enabled()` is true).
  - Reuse as-is: `services/publish_now.py` — `publish_due_jobs`,
    `auto_publish_enabled`, `set_auto_publish_enabled` (UI-pref-backed),
    `_PUBLISH_LOCK` (already serializes), account checkpoint cooldowns and
    one-retry logic (landed 2026-06-10).
  - Start/stop the thread at app lifetime: `src/nicheflow_studio/app/webview_app.py`
    ONLY. **PyQt is legacy-frozen (decision 2026-06-11): never add features
    there.** Its old timer stays as-is until the retirement sweep deletes it;
    just don't run both UIs at once during testing.
  - React: `ProcessingScreen.tsx` — remove the front-end `setInterval` loop;
    keep the toggle (it just flips the shared pref via the existing bridge
    methods `getAutoPublish`/`setAutoPublish`) and keep the due-count display.
    Update the toggle's confirm copy: posting no longer requires this screen
    to stay open, only the app itself.
- **How:**
  1. Write the loop service: `threading.Thread(daemon=True)`, `Event`-based
     stop, tick = check pref → if enabled call `publish_due_jobs()` → log the
     summary dict when posted+failed > 0. Sleep via `Event.wait(60)` so stop
     is immediate.
  2. Guard against overlap: `publish_due_jobs` already takes `_PUBLISH_LOCK`,
     but skip a tick if the previous one is still running (non-blocking flag).
  3. Start it in `webview_app.py` after the DB is ready; stop on shutdown.
  4. Strip the JS interval from `ProcessingScreen.tsx` (keep `publishDueNow`
     for the manual button).
- **How to test (pass criteria):**
  1. *Reproduce the bug first:* with the current build, enable auto-publish,
     schedule a dry-run job due in 1 min, navigate to another screen → job
     never posts. This is the "before" evidence.
  2. *Automated:* new `tests/test_auto_publish_loop.py` with a fake
     `publish_due_jobs`: (a) pref ON → called once per tick; (b) pref OFF →
     never called; (c) stop event terminates the thread < 1 s; (d) a tick
     that starts while the previous is running is skipped, not queued.
  3. *Manual after:* same steps as 1 → the job posts within ~90 s while on a
     DIFFERENT screen; publish-queue row flips to posted/dry-run and the log
     records the summary.
  4. *Regression:* `.venv\Scripts\python.exe -m pytest tests -q` fully green;
     manual "Publish due now" button still works; only ONE loop runs (check
     logs for duplicate tick lines when the PyQt window is also open).

---

## WO-2 — Title/caption hook upgrade (curiosity gap + comment bait)

**Priority: 2.** Zero-cost engagement lift; the model is fine, one rule is wrong.

- **What:** The `history_lost_archive` title rules in
  `src/nicheflow_studio/processing/smart_drafts.py` →
  `_caption_style_title_rules()` (~line 1369) HARD-BAN subject-withholding
  ("Weak (BANNED — hides the subject): 'The accessory that disappeared'…").
  That ban also kills the highest-performing competitor pattern (redhistory_:
  "She Performed What Would Become One of The Hardest Rap-Opening of All
  Time…" — 13.1K likes by withholding WHO). Own-account data agrees: the
  emotional title is the 17.1K outlier; flat factual summaries sit at 500–900.
- **Why:** Goal is audience growth → engagement. Titles that resolve the story
  in the title give no reason to watch/comment. Controlled curiosity ≠ vague
  mystery bait; the fix is a *scoped* exception, not deleting the ban.
- **Where:** `smart_drafts.py` `_caption_style_title_rules("history_lost_archive")`
  block only (the shared `_hook_drama_and_fact_safety_rules` green/yellow/red
  tiering stays untouched). Mirror text reaches the Copy Chat Prompt path
  automatically via `effective_title_rules` — verify both paths emit the new
  rules (`tests/test_smart_drafts.py` has prompt-content tests to extend).
- **How:**
  1. Add a CURIOSITY GAP shape rule: "use for EXACTLY one of the three
     options: withhold exactly ONE element — the subject ('She…', 'This
     12-year-old…') OR the outcome — while everything else stays concrete
     (era, action, stakes). The withheld element must be delivered by the
     clip in the first seconds. Trailing ellipsis allowed. Example: 'She
     performed what would become one of the hardest rap openings ever…'."
  2. Keep the existing BANNED list but reword it to ban *double*-withholding
     (subject AND outcome both hidden = vague mystery bait) instead of any
     withholding.
  3. Strengthen the COMMENT HOOK shape from "at most one option" to "at least
     one of the three options must use a question or direct-address form".
  4. Update the rotation list so the three options cover: one curiosity-gap,
     one comment-bait/question, one story-opener (the current default voice).
  5. Add few-shot winners: a short static block of 3–5 of the account's real
     top captions/titles (hand-picked from insights: the Janet Jackson VMA
     17.1K post etc.) injected for `history_lost_archive`. Plumb as a
     constant first; per-account DB-backed examples arrive with WO-6.
- **Constraint (CRITICAL):** the paste-parser plain-text output contract
  (`Title Option N:` headers etc., see `.claude/CLAUDE.md`) must NOT change.
- **How to test (pass criteria):**
  1. *Automated:* extend `tests/test_smart_drafts.py` prompt-assembly tests —
     the history-style rules text contains the curiosity-gap rule, the
     at-least-one-question rule, and the few-shot block; BOTH the live-Groq
     prompt and the Copy Chat Prompt emit them (they share
     `effective_title_rules` — assert on both outputs so they never drift).
     `tests/test_draft_handoff.py` stays green (paste contract unchanged).
  2. *Output check (one sitting):* generate drafts for 3 pooled clips. Pass =
     every generation has exactly ONE subject/outcome-withholding option,
     ≥ ONE question/direct-address option, ZERO options hiding both subject
     and outcome, ZERO banned phrases ('you won't believe', 'shocking',
     'changed history forever'). Paste one result through "Paste Draft from
     Clipboard" → parses cleanly.
  3. *The real test — engagement (2-week window):* post ~10 reels using
     new-style titles. In IG Insights sort by accounts-engaged and compare
     their median against the last ~10 old-style posts (baseline from the
     2026-06-10 screenshot: top 17.1K, mid-tier 500–900). Pass = the median
     clearly shifts up and at least 2 posts beat the old mid-tier ceiling.
     If flat after 10 posts, revisit which shape underperforms (the option
     notes record each option's angle — track which option you picked).

---

## WO-3 — Schedule-on-distribute

**Priority: 3.** Depends on WO-1 (posts must fire without the screen open).

- **What:** `services/pooling.py` → `distribute_clip` / `distribute_niche`
  assign pool items to accounts but never touch the calendar; every export
  still needs a manual auto-schedule click per item. Add an opt-in flag so
  committing a distribution also books each assigned item into its account's
  next open slots.
- **Why:** At 4–5 slots/day × several accounts the per-item click is the
  workflow bottleneck; the scheduling primitives already exist and are tested.
- **Where:** `services/pooling.py` (distribution commit), reuse
  `services/publishing.py::auto_schedule_for_publish` logic — factor its
  occupied-slot query + `next_open_slot_time` call into a helper that takes
  (account, count) and returns N slot times, then create the UploadJobs the
  same way `queue_for_publish` does. Note: scheduling requires an exported
  file (`processed_path`); pool items fresh from distribution are NOT
  exported yet — so the correct v1 shape is: schedule-on-distribute applies
  to items that are already exported, and for the rest, auto-schedule fires
  when the export completes (hook the end of the export service,
  `services/export.py`, behind a per-account "auto-schedule exports" flag).
- **How:** add `auto_schedule_on_export: bool` column to `Account`
  (`db/models.py` + migration consistent with how existing columns were
  added — check `db/session.py` for the lightweight migration pattern), set
  it from the account settings UI, and at export completion call
  `auto_schedule_for_publish(item_id)` when the flag is on. Surface failures
  (no slots configured) as a non-fatal warning in the export result.
- **How to test (pass criteria):**
  1. *Automated:* export completion with flag ON → exactly one `UploadJob`
     status `scheduled` at the account's next OPEN slot (existing scheduled
     jobs respected — no two jobs within the collision window); flag OFF →
     no job; flag ON + no slots configured → export still succeeds and the
     result carries a warning string.
  2. *Manual:* enable the flag on a test account with slots set, export one
     item → job appears in the Publish Queue with a future slot time that is
     NOT on a round minute (jitter applied). Export a second item → it lands
     on the NEXT slot, not the same one.

---

## WO-4 — Per-account daily cadence field

**Priority: 4.** Small, pairs naturally with WO-3's account-settings touch.

- **What:** Replace global `DEFAULT_DAILY_POSTS_PER_ACCOUNT = 4`
  (`core/distribution.py`) as the only source of backlog targets with an
  optional `daily_posts_target` column on `Account` (None → global default).
- **Why:** pastmomentsdaily has earned 4→5/day (see ramp plan in the backlog);
  brand-new accounts should run 2–3. One global constant can't express that.
- **Where:** `db/models.py` (column), `core/distribution.py::target_backlog`
  callers (find them: grep `target_backlog(`), account settings UI field,
  `services/accounts.py` serialization (mirror `upload_schedule_slots`).
- **How to test (pass criteria):**
  1. *Automated:* two accounts, targets 3 and 5, planning window 7 →
     distribution fills them to 21 and 35 respectively; account with target
     None falls back to the global default (4 × 7 = 28).
  2. *Manual:* set pastmomentsdaily to 5 in account settings, run "Top Up"
     in Pooling → the preview shows its backlog target as 35 and other
     accounts unchanged.

---

## WO-5 — Capture extension: side panel + editable queue

**Priority: 5.** Owner-requested UX; independent of the Python backend.

- **What:** The popup auto-closes on focus loss (Chrome platform rule — no
  workaround exists for popups). Rebuild the capture UI on the
  `chrome.sidePanel` API so it stays docked while scrolling reels. Add:
  (a) queue list with per-item remove, (b) queueing stays usable while an
  Apify batch is processing, (c) keep pool/estimate/monthly counters.
- **Why:** Current flow forces popup-reopen per reel; batch processing blocks
  further queueing. This is pure throughput for the sourcing session.
- **Where:** `browser-extension/nicheflow-capture/` — `manifest.json` (add
  `"side_panel"` + `sidePanel` permission, keep the action popup as a thin
  "open panel" shim or drop it), move popup HTML/JS to the panel page. The
  native-messaging host contract (`scripts/install_capture_extension.ps1`,
  the queue/process messages) stays unchanged — only the UI surface moves.
- **How:** queue state lives in `chrome.storage.session` (or local) so the
  panel and background worker share it; "Process Queue" snapshots the current
  queue into a batch and clears only those items, so new queueing during the
  batch is unaffected; render batch status + last result in the panel.
- **How to test (pass criteria — manual checklist, no automated harness):**
  1. Panel stays open while clicking into the page, scrolling reels, and
     switching tabs (the old popup failed all three).
  2. Queue 3 reels → all 3 listed in the panel with remove buttons.
  3. Click "Process Queue", then DURING the batch queue 2 more reels and
     remove 1 of them → batch completes with the original 3; queue shows the
     1 remaining new item; counters (pool size, Apify estimate, monthly)
     update.
  4. *Regression — host contract unchanged:* queue 1 reel, process, confirm
     it appears in the target pool in the app and the badge/notification
     still reports added/duplicate/failed.

---

## WO-6 — Performance feedback loop (metrics → ranking → few-shot)

**Priority: 6.** Foundation for "results get better over time".

- **What:** `UploadJob` already has `posted_views/likes/comments/shares`
  columns that nothing fills or reads. Build: (1) a manual per-post metrics
  entry UI (paste numbers from IG insights — no scraping), (2) feed
  per-account winners into `engagement_score`-style ranking weights at
  distribution, (3) replace WO-2's static few-shot block with the account's
  actual top N titles/captions pulled from these columns.
- **Why:** Closes the loop: the system currently ranks clips only by the
  SOURCE post's likes; it never learns what works on OUR accounts.
- **Where:** publish queue UI (metrics entry), `core/distribution.py`
  (ranking already has seams: `engagement_score`, `ranked_clip_order`),
  `smart_drafts.py` few-shot injection (WO-2 step 5 plumbing).
- **How (v1, keep small):** metrics entry + "top posts" query
  (`services/publishing_dashboard.py` likely has the right home — read it
  first) + few-shot from winners. Defer ranking-weight learning until ≥
  ~50 posts have metrics; note that in code comments.
- **How to test (pass criteria):**
  1. *Automated:* top-posts query returns posted jobs ordered by engagement
     for ONE account (never mixes accounts/niches); jobs without metrics are
     excluded; few-shot block renders the top N titles.
  2. *Manual:* enter insights numbers for 3 posted reels, regenerate a draft
     for that account, click "Copy Chat Prompt" → the prompt visibly contains
     those 3 winners as style examples. An account with no metrics falls back
     to the static WO-2 examples (no crash, no empty block).

---

## WO-7 — Writer model swap (DeepSeek V3 / Kimi K2 writer tier)

**Priority: 7.** Do AFTER WO-2; measure prompt gains before paying for a model.

- **What:** Add an OpenAI-compatible writer provider (DeepSeek API,
  ~$0.27/M in $1.10/M out) selectable ahead of Groq in the writer fallback
  chain. Vision stays on Groq Llama 4 Scout (DeepSeek V3 is text-only).
- **Why:** Llama 3.3 70B is the free-tier ceiling for casual social English;
  DeepSeek/Kimi write it noticeably better at pennies/month for this volume.
- **Where:** `processing/smart_drafts.py` — `_resolve_provider_order` (~line
  166) already implements a provider chain (groq → ollama); add `deepseek`
  as a provider that reuses the OpenAI-compatible chat-completions request
  shape (Groq's URL/format is already OpenAI-compatible — factor the request
  builder so both share it). Env: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`
  (default `deepseek-chat`). Budget guard: mirror the existing Groq monthly
  budget pattern (`DEFAULT_GROQ_MONTHLY_BUDGET_USD`).
- **Constraint:** vision/frame analysis path keeps using Groq; only the
  writer call routes to DeepSeek when the key is set. JSON-output contract
  and paste format unchanged.
- **How to test (pass criteria):**
  1. *Automated:* provider order includes `deepseek` first when
     `DEEPSEEK_API_KEY` is set, absent otherwise; request-payload golden test
     (model name, JSON-mode flag, same messages as Groq); vision calls still
     route to Groq with the key set; budget guard blocks past the cap.
  2. *Manual A/B (the decision test):* pick 5 pooled clips, generate each
     with Groq then DeepSeek (same clip, same style). Blind-compare the 10
     results. Pass = owner prefers DeepSeek on ≥ 3/5 → keep it; otherwise
     remove the key and stay on Groq (the chain makes this a no-op).
  3. *Cost check:* after the A/B, recorded spend matches expectations
     (~$0.001–0.005/clip); the usage counter mirrors the Groq pattern.

---

## WO-8 — Visual near-duplicate guard at pool intake

**Priority: 8.**

- **What:** URL/shortcode dedup exists at intake (`db/pool_intake.py`); two
  *different* posts of the same underlying clip still pass and can land on
  two network accounts. Wire the existing visual-dedup pass
  (`processing/dedup.py` — read it first; it was built for the library) into
  pool intake as a warn-or-reject.
- **Why:** Cross-account duplication is the main originality/correlation risk
  the distribution design works to avoid; this is the last open hole.
- **Where:** `db/pool_intake.py` + `processing/dedup.py`. Constraint: intake
  happens before download in the Apify flow (metadata only) — so v1 runs the
  visual check at DOWNLOAD time (when bytes exist) and flags the pool item
  (`near_duplicate_of` reference) for the review UI rather than hard-reject.
- **How to test (pass criteria):**
  1. *Automated:* two visually-identical fixture videos → second is flagged
     with `near_duplicate_of` pointing at the first; two distinct fixtures →
     both clean. (Reuse whatever fixtures `tests/` already has for
     `processing/dedup.py`.)
  2. *Manual:* download a pool item, then queue a different IG post known to
     contain the same clip → after download it shows a duplicate flag in the
     review UI and is NOT distributable until the owner clears it.

---

## WO-9 — Cross-account slot stagger

**Priority: 9.** Interim hand-set slot offsets per account work fine today.

- **What:** Warn (account settings + dashboard) when two accounts have slots
  within 15 min of each other; later, auto-suggest offsets. Note WO-1's
  randomized 2–6 min inter-post gap already prevents literal back-to-back
  posting even when slots collide — this is defense-in-depth, not urgent.
- **Where:** `core/scheduling.py` (pure helper: pairwise slot-distance check
  across accounts), surfaced in `services/accounts.py` + settings UI.
- **How to test (pass criteria):** unit: accounts with `09:00` and `09:05` →
  flagged pair returned; `09:00` and `13:00` → clean; empty/None slots
  ignored. Manual: set two accounts 5 min apart → warning visible in
  account settings and dashboard; fix the offset → warning clears.

---

## WO-10 — Single-instance guard

**Priority: 10.**

- **What:** Two app instances could drive two browsers concurrently
  (publish lock is in-process only). Add a file lock under `data/` taken at
  startup (`msvcrt.locking` or a lock-dir approach — Windows-only MVP);
  second instance gets a clear "already running" dialog and exits.
- **Where:** `app/webview_app.py` startup (and PyQt entry if kept).
- **How to test (pass criteria):** launch the app, then launch a second
  instance → second shows "already running" and exits; first instance is
  unaffected and can still publish. Kill the first instance hard (Task
  Manager) → a fresh launch succeeds (stale lock is reclaimed, not fatal).

---

## WO-11 — Automatic database backup (gap found 2026-06-11)

**Priority: do alongside WO-1 — smallest job in the plan, protects everything.**

- **What:** The zip backup exists only as a MANUAL button in the legacy PyQt
  window (`app/main_window.py` ~10408, writes `nicheflow-backup-*.zip` to
  `data/backups/`). The React/webview app — the migration target — has no
  backup path at all, and nothing is automatic.
- **Why:** The SQLite DB now holds the whole business: ~1,700 pooled clips,
  accounts, assignments, the publish queue, and draft history. One corrupt
  write or accidental `data/` cleanup loses it. Cost of the fix: ~an hour.
- **Where:** new small service `services/db_backup.py`; call it from
  `app/webview_app.py` startup (after DB ready, before the UI loads). Reuse
  `core/paths.py::backups_dir()` and the existing
  `nicheflow-backup-YYYYMMDD-HHMMSS.zip` naming so the PyQt manual button
  and the auto path share one folder/retention. Find the DB filename in
  `db/session.py` rather than hardcoding.
- **How:** on startup, if the newest backup is older than 24 h, write a new
  zip (use `sqlite3` `Connection.backup()` into a temp file, then zip — safe
  even if connections are open) and prune to the newest 14. Log one line.
  Never let a backup failure block app startup — warn and continue.
- **How to test (pass criteria):**
  1. *Automated:* fresh start with empty `backups/` → one zip appears;
     restart within 24 h → no second zip; seed 15 fake `nicheflow-backup-*`
     zips → pruned to 14 newest; backup-write failure (read-only dir) →
     startup still succeeds with a warning log.
  2. *Restore drill (manual, once):* close the app, rename the live DB,
     unzip the newest backup into place, relaunch → accounts/pools/queue all
     present. A backup that has never been restore-tested is not a backup.

---

## WO-12 — Packaged webview build validation

**Priority: after WO-1 + WO-11 land (they change app startup — validate once,
not twice).** Packaging is MVP, not post-MVP (`.claude/CLAUDE.md`).

- **What:** Validate that the React/webview app — including the new backend
  auto-publish loop and auto-backup — works as a packaged build outside the
  dev environment. The scripts exist (`scripts/build_webview.ps1`,
  `scripts/smoke_packaged.ps1`, `scripts/run_fresh_packaged.ps1`,
  `NicheFlowStudio.spec`); what's missing is a validated pass over the
  current feature set, per `STATUS.md`'s standing goal.
- **Why:** Everything in this plan only counts when it runs from a packaged
  exe on a machine without the venv. Startup-time features (WO-1 loop,
  WO-11 backup) are exactly the kind that break under PyInstaller (threads,
  paths, missing data files) — so validate right after they land.
- **Where:** the build scripts above + whatever they surface. Fix root
  causes in `src/`, not by patching the dist output.
- **yt-dlp staleness (found 2026-06-11):** IG downloads fail with "Instagram
  sent an empty media response" when yt-dlp ages (extractor broke at ~3
  months stale). Dev runs now auto-upgrade via `scripts/dev_webview.ps1`,
  but a PyInstaller-frozen lib CANNOT pip-update itself — the packaged build
  must either (a) ship yt-dlp as a sidecar `yt-dlp.exe` invoked by
  subprocess with its own `-U` self-update, or (b) check PyPI at startup and
  tell the user to update the app. Decide during this WO; (a) is the
  standard solution and also future-proofs the YouTube path.
- **How to test (pass criteria — the packaged smoke checklist):**
  1. `scripts/build_webview.ps1` completes clean.
  2. `scripts/run_fresh_packaged.ps1` (fresh `data/` dir): app launches,
     a backup zip appears (WO-11), accounts screen loads.
  3. Core loop in the packaged app: import one clip → generate drafts →
     export → auto-schedule → dry-run publish fires from the BACKEND loop
     (WO-1) with the Processing screen closed.
  4. Close + relaunch: state persists; no duplicate backup within 24 h.
  5. Document any step that needed a workaround as a follow-up fix.

---

## WO-13 — Missed-slot catch-up scheduling

**Priority: with WO-3 (same code area; can also ship standalone before it).**
Owner-reported 2026-06-11: exported at 09:23, account slots are
09:00/13:00/17:00/21:00, last post was hours earlier — auto-schedule skipped
the unused 09:00 slot and booked 13:02, silently turning a 4-post day into 3.

- **What:** `core/scheduling.py::next_open_slot_time` only looks forward; it
  has no concept of "a slot just passed and nobody used it". Add catch-up: if
  a recent slot went unused, schedule at `now + random(5–20 min)` instead of
  waiting for the next slot.
- **Why:** Posting cadence is the consistency lever; every silently-skipped
  slot is a lost post. Catch-up recovers it while still looking human (small
  randomized delay, never instant).
- **Guardrails (all must hold, else fall back to the next forward slot):**
  1. The missed slot passed within a GRACE window (default 3 h — late enough
     to matter, never reaching back to yesterday's slots).
  2. The missed slot is genuinely unused: no job (scheduled or posted) within
     the collision window of that slot moment.
  3. Real gap since the account's last ACTUAL post: `now - max(posted_at)`
     ≥ MIN_GAP (default 2 h) — never lets catch-up stack onto a recent post.
  4. Nothing else already scheduled between now and the next forward slot.
  5. Account not in checkpoint cooldown. NEVER auto-fire "publish now" —
     catch-up always schedules with the randomized delay; the existing
     publish loop posts it.
- **Where:**
  - `core/scheduling.py`: new pure helper `catch_up_slot_time(slots, *,
    now, occupied, last_posted_at, grace_hours=3, min_gap_hours=2, rng)` →
    returns `now + random(5–20 min)` or `None`. Keep it pure (no DB) like the
    rest of the module.
  - `services/publishing.py::auto_schedule_for_publish`: try catch-up first
    (it already loads the occupied list; additionally fetch the account's
    latest `posted_at`), fall back to `next_open_slot_time`.
  - Surface which path was taken in the result dict so the UI can say
    "Catch-up: scheduled for 09:41 (missed 09:00 slot)".
- **How to test (pass criteria):**
  1. *Automated (pure, seeded rng):* slots 09:00/13:00, now=09:23, last post
     yesterday, no jobs → returns 09:28–09:43 window. Same but last post
     08:30 → `None` (min-gap). Same but a job already posted at 09:05 →
     `None` (slot used). Now=14:00 with 09:00 missed → `None` (grace
     expired; 13:00 missed → catch-up). Job already scheduled 10:00 →
     `None` (rule 4). Service-level: catch-up result creates the job with
     the catch-up time; guardrail failure falls through to 13:00+jitter.
  2. *Manual (the original repro):* export an item after a slot has just
     passed unused → UI shows "Auto-scheduled for ~now+5–20 min" instead of
     the afternoon slot; export a second item immediately → it goes to the
     next forward slot (rule 4 prevents double catch-up).

---

## WO-14 — White-canvas crop accuracy (owner-reported 2026-06-11)

**Priority: high — first quality item after WO-2's hook work; it degrades every
white-canvas export (visible white margins around the footage).** Black-canvas
reels crop tight; white-canvas reels keep a white halo.

- **What:** `processing/video.py::detect_content_rectangle` under-crops when
  the surrounding canvas is white/light. Root cause is a dark-canvas
  assumption in two places:
  1. `CONTENT_RECT_SHARP_PIXEL_THRESHOLD = 1.0` (|Laplacian| on 0–255 luma)
     is far below white-canvas compression noise. Encoders write black canvas
     as flat zeros, but white canvas carries banding/shimmer (±1–3 luma), so
     canvas pixels register as "sharp". The row band extension
     (`motion OR sharp`, ~line 620) and the column signal
     (`slab_sharp_cov`, ~line 651) then absorb white margins as content.
     The stride-based `_downscale_frame` preserves that noise (no averaging).
  2. The no-motion fallback `brightness > CONTENT_RECT_BRIGHTNESS_THRESHOLD
     (46)` literally defines canvas as "dark" — a white canvas is all
     "content" on that path.
- **Why:** Crop quality is output quality — a white halo looks like a lazy
  repost and wastes vertical pixels that the title band needs. Memory note:
  `detect_content_rectangle` is the single crop authority at export
  (see decisions memory / Crop authority) — fixing it fixes every export.
- **Where:** `processing/video.py` only — constants block (~line 366) and
  `detect_content_rectangle`. Do NOT touch the vision `content_box` path
  (metadata-only by decision) or `suggest_title_replacement_crop`'s contract.
- **How (canvas-aware, not threshold-whack-a-mole):**
  1. Estimate the CANVAS COLOR from the frame's outer border pixels
     (e.g. 2% frame margin, median luma across sampled frames) instead of
     assuming dark.
  2. Build a canvas mask: pixels within a tolerance (~8–12 luma) of the
     canvas color AND below the motion threshold. Exclude canvas-mask pixels
     from `sharp_pixel` before any row/column coverage is computed — this
     kills the white-shimmer false positives without raising the global
     sharp threshold (which would regress dark-banner detection like the
     COURTROOM case documented in the comments).
  3. Make the no-motion fallback canvas-relative: content = |luma − canvas|
     > threshold, not brightness > 46.
  4. Keep every existing special path (blurred-bg, overlay-bar, text-top
     scan, descender padding) byte-identical for dark canvases — the
     regression risk here is the carefully-tuned dark cases in the comments.
- **How to test (pass criteria):**
  1. *Automated fixtures (ffmpeg-generated in the test, like existing video
     tests):* embed a moving noise/testsrc rectangle at a KNOWN position
     into (a) black canvas, (b) white canvas, (c) off-white #F5F5F5 canvas,
     encoded libx264 CRF 28 (realistic shimmer). Assert the detected
     rectangle is within ~2% of the known footage rect for ALL three.
     Today (a) passes and (b)/(c) fail — that asymmetry IS the bug.
  2. *Regression:* full suite green; any existing crop/export golden tests
     unchanged for dark-canvas inputs.
  3. *Manual:* re-run "Adjust crop"/re-export on the white-canvas reel from
     the 2026-06-11 report (Princess Diana / Prince William clip) → exported
     reel shows footage edge-to-edge with no white margin; spot-check 2
     known-good black-canvas reels still crop identically.

---

## Later / explicitly sequenced

- **Assisted discovery (extension scoring)** — after WO-5; passive collection
  of metadata while the owner scrolls, like-velocity ranking. Hard rule
  stands: no auto-scroll botting in a logged-in browser.
- **Pipeline stages 1→3 (auto-prepare → auto-schedule → auto-publish)** —
  after WO-1/2/3/6; owner approval of video+title+caption stays the gate
  until explicitly lifted per account.
- **Legacy cookie-scraping deletion (P3)** — verify no UI path reaches
  `scraper/instagram.py`, then delete; any time, low risk.
- **PyQt retirement sweep (decision 2026-06-11: React/webview is THE app)** —
  PyQt gets zero new features from now on. One pass to (1) inventory
  anything that still exists only in `app/main_window.py` (known: manual
  backup button — superseded by WO-11; runtime-paths page ~line 10373 —
  port as a small settings card if wanted; check the schedule page),
  (2) port the keepers to React, (3) then delete `main_window.py` and its
  PyQt-only deps from the packaged build. Deleting ~12k lines also shrinks
  the package and the test surface.
- **Repo hygiene (5 min)** — `debug-generate-drafts.log` and the
  `*.png` UI screenshots sit at the repo root untracked-or-committed; move
  to `data/` or delete and extend `.gitignore` so debug artifacts can't land
  in commits.
- **Instagram Graph API publisher — LAST**, when the network is bigger.
  Settle the portfolio-grouping question first (see backlog).

## Standing constraints for every work order

- Windows-only desktop MVP; data under `data/` stays gitignored.
- No new dependencies without justification (DeepSeek uses stdlib/requests
  same as Groq — no SDK).
- Paste-parser plain-text contract is inviolable (`.claude/CLAUDE.md`).
- Smallest relevant verification after each WO; suite must stay green
  (`.venv\Scripts\python.exe -m pytest tests -q`; bare `python` is 3.6).
- Conventional commits, one WO per commit/PR where possible.
