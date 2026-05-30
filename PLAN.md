# NicheFlow Studio Plan

Last updated: 2026-05-24
Status: Active execution plan
Current milestone: Make the Instagram-first manual publishing MVP coherent on top of the working scrape/download/processing flow. Niche account strategy locked in (see §2A).

## 1. What This Project Should Be Right Now

NicheFlow Studio should ship first as a small, reliable Windows desktop app for a multi-account clipping workflow.

The long-term product is not just a downloader. It is a system that helps a user:

1. choose and manage a content account/profile
2. acquire content for that account
3. build and manage a local library of candidate clips
4. prepare Instagram-ready Reels for manual publishing
5. track posted Reels and basic performance manually

For the current MVP, only the first reliable slice needs to be finished:

- account management
- account selection
- YouTube / YouTube Shorts acquisition as a source pipeline
- Instagram manual source intake for Reel/profile/hashtag references
- Instagram-ready Processing output
- manual Instagram Publish Queue
- download history and local file tracking
- packaged Windows delivery

Current strategy:

- Keep the account -> scrape -> download -> process flow coherent.
- Make Instagram the publishing target without requiring Meta API approval.
- Keep YouTube as an intake/download source, not the publishing center of gravity.
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
- YouTube / YouTube Shorts ingestion via `yt-dlp` as a source pipeline
- Instagram manual source intake for pasted Reel/profile/hashtag references
- download queue with background workers
- local library/history with status, retry, remove, open, and review actions
- Processing export optimized for Instagram Reels
- manual Instagram Publish Queue with copy-caption, open-output, mark-posted, and metric tracking
- enough packaging work to run the app outside the dev environment

### Explicitly Deferred

These are part of the broader vision, but should not drive current implementation:

- TikTok ingestion
- automated smart scraping/discovery across multiple sources
- official Instagram Graph API intake or publishing
- logged-in Instagram scraping or bot automation
- automatic Instagram media downloading/reposting without rights
- broad processing automation beyond the current title/crop workflow
- AI caption generation, embeddings, niche scoring, virality scoring, or drift detection
- analytics dashboards
- cloud sync
- stealth / anti-detection systems

## 2A. Niche Account Strategy

Last updated: 2026-05-24
Status: Active — drives account setup, content choices, and campaign eligibility decisions.

### Account portfolio (5 Instagram accounts, dual-use)

Each account does double duty: it is both a niche brand account (where we publish)
AND a scraping profile in the `ProfilePool` (where we read public posts from).
Same IG account, two roles. Trade-off accepted: fewer accounts to manage,
slightly higher per-account ban risk.

Throughput math at 5 dual-use accounts: scraping ceiling drops from ~2,500-3,500
posts/day (separate pools) to ~1,000-1,500 posts/day. Still far more than any
single niche needs, so net-positive trade-off.

### Confirmed niches (4 of 5)

| # | Niche | Lane (the specific lane within the niche) | Caption style preset | Title style preset |
|---|---|---|---|---|
| 1 | Meme | Chronically online late-night burnout humor — 3am cope, doomscrolling, anxiety humor, online-culture meta. NOT generic memes. | `meme_relatable` | `meme_setup_punchline` |
| 2 | History | Specific lane TBD (ancient civ / war stories / forgotten figures / weird facts — pick ONE) | `narrative` | Auto / Narrative |
| 3 | Movie | Cinema atmospheric — scene reactions, plot-twist moments, IMDb-style synopsis body. Modelled on @cinema.defined. | `cinema_hook` | `cinema_hook` |
| 4 | Music | Specific lane TBD (lyric breakdowns / song moments / artist BTS / genre-specific) | TBD | TBD |
| 5 | — | TBD (on hold — only add when a niche genuinely interests the user) | — | — |

### Core principle: one account = one specific lane

People don't follow accounts for broad categories ("memes"). They follow for a
predictable vibe consistently delivered. If they can't guess what the next post
will feel like, they don't hit follow.

Filter rule for every post: "Does this hit harder for someone in MY lane than
for the general population?" If yes → post. If no → skip even if it's good
content on its own.

This applies to campaign clips too: when a campaign provides 30 source clips,
only post the 3-6 that fit the account's lane. Frame every clip with a hook
in the account's voice via the text overlay.

### Campaign category → niche compatibility

Campaign platform offers categories: irl-streaming, irl-content, gaming,
sports, music, podcasts, gambling. Gambling explicitly avoided (IG restrictions
+ user preference).

| Niche | Direct campaign category fits | Indirect fits (clip-by-clip) |
|---|---|---|
| Meme | None | Podcasts (funny moments), irl-streaming (viral fails), occasional sports |
| History | None | History-themed podcasts only (Hardcore History, Lex Fridman + historians) |
| Movie | None | None — organic-growth-only on this platform |
| Music | Music | — |

Only Music has a direct category. Other niches participate by cherry-picking
clips from podcast/irl campaigns that match the account's lane. Movie account
is an organic-growth play (not campaign-eligible on the current platform).

### Account growth gating (must be hit before campaigns)

Most clipper platforms require minimums before letting an account submit:
- 1,000+ followers
- 30+ days account age
- Real engagement history (not zero-like posts)
- Audience demographics matching campaign requirements (e.g. 50%+ English-speaking
  countries — language of captions is NOT the same as follower geography;
  this must be tracked in IG Insights once each account is active)

Implication: all 5 accounts go through a 4-8 week growth phase before any
campaign monetization is realistic.

### @memeistsdaily — first account, current status

- Lane: chronically online late-night burnout humor
- Status (2026-05-24): 14 posts, 1 follower — pre-monetization growth phase
- Bio (committed): `your daily dose of cope 💀 / new clip every 24h / ↓ turn on notifications`
- Display name suggestion: `daily memes 💀`
- Profile pic: new neon-green mascot on pure black (high-contrast, thumbnail-readable)
- Next steps: update bio + display name on IG, post daily in the committed lane

### NicheFlow Account-row config to mirror the strategy

When each niche account gets a row in the `accounts` table, the
caption/title style fields above feed straight into `smart_drafts.py`. The
free-text Account fields (`writing_tone`, `target_audience`, `hook_style`,
`banned_phrases`, `title_style_notes`, `caption_style_notes`) tighten the
prompt so generated captions stay in lane automatically. These should be
filled per-account, not left blank.

### Campaign-platform rules to respect (clipper side)

Live constraints from the active payout platform — must be obeyed across
all 5 accounts:

- No botting / fake engagement (no view pumps, like-for-like pods, comment rings)
- No low-quality / "blatantly automated" posts — AI-generated captions must not
  read as AI. Style work in `smart_drafts.py` exists precisely to clear this bar.
- No repeat posts on the same account
- Posts must stay public until payment lands
- Per-post min: 1,000 views. Total min for eligibility: ~25,000 views.
- Audience-match: campaigns may require 50%+ followers from English-speaking
  countries. Posting in English does not guarantee this — depends on who
  IG distributed the early posts to. Bias early posts toward US-timezone
  signals (US trending audio, US-time-zone posting, engaging with US accounts).
- Vary hook templates across posts — don't ship 10 reels starting with "When
  you realize…". Bot-detection looks at template repetition.
- Add ±15-min jitter to posting time — don't post at 20:00:00 exactly every day.

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

### Flow C: Instagram-First MVP Flow

This is the current MVP target after the Instagram pivot:

1. choose an Instagram account/niche such as `RespawnReels`
2. collect candidate ideas from YouTube sources and manually entered Instagram references
3. download/process only selected clips where the user has an appropriate reuse path
4. export a vertical Instagram-ready Reel with the black-canvas/no-blur template
5. copy the caption and manually publish in Instagram
6. mark the job posted and record posted URL plus basic metrics

The MVP should work without Instagram API approval. Official Meta/Instagram API integrations come after the manual queue proves useful.

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
- [ ] account-fit analyzer for Instagram source accounts
- [ ] source-to-carousel/post generator for archive-style post accounts
- [ ] direct manual intake from a single YouTube / Shorts URL such as `https://www.youtube.com/shorts/...`
- [ ] stronger duplicate/content-fit rules
- [ ] scheduling
- [ ] uploaders
- [ ] analytics

Planned order after the current Reels MVP:

1. Account Fit Analyzer
   - Compare scraped or manually captured Instagram source accounts against managed account profiles such as `Life Lagged`, `MemeistsDaily`, and future accounts.
   - Score source-account fit by niche, tone, audience, format, content pillars, engagement quality, and reuse risk.
   - Extract useful management insights from scraped metadata: common hooks, recurring topics, top formats, posting cadence, and which sources are approved, rejected, or review-only.
   - Keep the feature focused on learning and source selection; do not auto-repost Instagram media.

2. Source-to-Carousel/Post Generator
   - Support future post-first accounts such as `Mister Lost Archive`.
   - Treat Instagram posts as inspiration only: extract the idea, verify the story from external sources, and generate original single-post or carousel drafts.
   - Auto-search free/open visual sources first: Library of Congress, Wikimedia Commons, Europeana, Pexels, Pixabay, and later Flickr Commons if useful.
   - Generate AI fallback prompts when safe visuals are weak or unavailable; AI output should be marked internally as a visual recreation and should not be presented as a real historical photo.
   - Render a dark newspaper/archive-style template for single posts and carousel slides after the user approves the story and visual plan.

### Milestone 5A: Instagram-First Manual Publish MVP

Goal: Make the app useful for preparing and manually posting Instagram Reels before any API automation.

Tasks:

- [ ] clean up remaining YouTube-uploader-specific publish code paths
- [ ] keep the Publish Queue focused on manual Instagram posting
- [ ] ensure every processed Reel can be added to the Publish Queue
- [ ] copy caption from a selected publish job
- [ ] open the exported Reel from a selected publish job
- [ ] mark a selected publish job as posted
- [ ] record posted URL, posted time, views, likes, comments, shares, and content type
- [ ] add a posted/draft filter if the queue gets hard to scan

Definition of done:

- A user can process a clip, add it to the Publish Queue, manually post it to Instagram, mark it posted, and record basic results without any external API setup.

### Milestone 5B: Instagram Manual Source Intake

Goal: Let the user track Instagram source ideas safely without raw Instagram scraping.

Tasks:

- [ ] add source types for `instagram_reel`, `instagram_profile`, and `instagram_hashtag`
- [ ] allow Instagram URLs/hashtags as source references for selected accounts
- [ ] save Instagram candidate rows as metadata/manual notes, not downloaded media
- [ ] show Instagram candidates in the existing candidate review flow
- [ ] allow selected Instagram candidates to become planning references for Processing/Publish Queue work
- [ ] keep official Instagram Graph API support deferred until Meta setup is ready

Definition of done:

- A user can add Instagram Reel/profile/hashtag references, review them as candidate ideas, and keep them organized by account without automated Instagram scraping.

## 7. Immediate Priority Backlog

Last updated: 2026-05-24

### Just shipped (code done, tests passing) — 2026-05-24

- **`cinema_hook` caption + title style** (in `smart_drafts.py`)
  - New caption style modeled on @cinema.defined: hook line with ellipsis beat
    + one emoji (💀/🤯/💔) → 2-3 Wikipedia-style paragraphs opening with
    `"[Film Title] (Year), directed by [Director], is a [genre] film about [premise]."`
  - New title style: 10-20 word atmospheric sentence using three templates
    ("That kind of…", reveal-beat, short stab fragments)
  - Forces named-entity grounding (no "this guy / that movie" hedging when
    vision identified the film)
  - Movie / film / cinema niche profile added to `_niche_profile()` and
    `_angle_plan()` so prompts write like a cinephile, not a meme account
  - UI: "Cinema Hook" added to Caption Style dropdown, "Cinema Atmospheric"
    added to Title Style dropdown in `main_window.py`
  - Skip lists updated so encyclopedic-explainer bans don't fight the
    deliberately-encyclopedic synopsis body of this style

- **Rate-limit hardening for Instagram scraping** (in `scraper/instagram.py`
  and `core/instagram_profile_pool.py`)
  - Graduated cooldown: 1st 429 → 30min, 2nd → 2h, 3rd+ → 6h (was always 6h).
    3-5× higher daily scraping throughput across main/alt1/alt2 set.
  - Adaptive per-request delay: `1.5 ** min(failure_count, 3)` multiplier
    on profiles with recent 429 history — recovering profiles slow down so
    they don't crash back into another 429.
  - Wider base delay band: `(4.0, 8.0)` → `(6.0, 14.0)` for more human cadence.
  - Stale-session filter: `ProfilePool.available()` now auto-skips profiles
    whose login is >=14 days old, instead of burning a request to discover
    they're dead.
  - 9 new tests in `test_instagram_profile_pool.py` + `test_instagram_scraper_rotation.py`,
    plus the existing 25 still green.

### Just shipped (code done, tests passing) — earlier

- Prompt rewrite: profile-branched prompt (~50 lines), replaces 130-line mega-prompt
  - `gaming_meme` / `reaction_clip` → meme.ig style, situational POV hooks, 3-paragraph captions
  - `story_reel` → human-interest storyteller
  - `broad_short_form` → clean general short-form
  - Account `writing_tone` field now feeds a tone lean
- Caption paragraph spacing bug fixed (`\n\n` was being collapsed to a single space)
- Ollama caption dict-wrapping bug fixed (`_clean_options` unwraps `{"caption":"..."}`)
- **Auto smart crop fixed (verified end-to-end on the Bee Movie meme clip):**
  - New `detect_content_rectangle` in `video.py` — temporal-variance detection finds
    the embedded footage rectangle on all 4 sides (footage moves, canvas/text don't).
  - `suggest_title_replacement_crop` now uses it as the primary signal; the old
    45% cap that was discarding the correct answer is gone.
  - Vision payload now returns a `content_box` (footage rectangle in frame
    fractions) instead of the conservative 0.0 crop ratio.
  - Title-band export filter made deterministic (fixed-size content pad,
    even dimensions, resolution normalization) — fixes a VP9 mid-stream reinit crash.
  - Result: the leftover source title/sub-line text no longer survives the crop.

### Immediate next: repair real-clip generation quality

Verification status from 2026-05-20:

- [x] Groq connectivity works. `.env` loads the key and `scripts\test_generation.py --account 4 --limit 1` now reaches Groq.
- [x] Groq fallback root cause found for the first failed run: Groq returned model JSON with a literal newline/control character, the strict parser rejected it, and generation fell through to Local fallback.
- [x] Parser now accepts that common model-output issue after trying strict JSON first.
- [x] Real-clip smoke runs completed:
  - `.venv\Scripts\python.exe scripts\test_generation.py --account 4 --limit 5` -> `5 generated, 0 errors`
  - `.venv\Scripts\python.exe scripts\test_generation.py --account 2 --limit 5` -> `5 generated, 0 errors`
- [x] Structural title shape improved: outputs use "When you...", "Bro really thought...", and "POV:..." hooks.
- [x] Caption paragraph spacing and hashtag separation work.
- [x] Banned phrases are respected in the tested output.

Observed blockers:

1. Vision only ran on 2 of 10 real clips.
   - Vision present: item 30 and item 17.
   - Vision absent: 8 other clips, including items 29/31/32 with title `Video by meme.ig`, no transcript, and little usable metadata.
   - Without vision, writer-only Groq has almost no grounding and produces generic repeated hooks like "When you finally understand the joke".
   - When vision works, output becomes specific enough to use, for example `POV: You just got caught in Lecato's Elytra Drip Trap`.
2. Captions read like Wikipedia instead of meme.ig.
   - Current captions often start with definitions like "Minecraft is a popular sandbox game...".
   - The prompt is over-indexing on "explainer" and explaining concepts the target audience already understands.
   - Account metadata leaked verbatim once: `Gen Z gamers and meme fans are always on the lookout...`.

#### Fix A: make vision failures visible and reduce silent writer-only output

- [x] Add provider diagnostics to `SmartDrafts.generation_meta` when Groq vision fails or is skipped:
  - `vision_attempted`
  - `vision_used`
  - `vision_error`
  - `vision_retry_attempted`
  - `frame_count`
  - `low_context`
  - `writer_model`
  - `vision_model`
- [x] In `scripts\test_generation.py`, print `vision_error` and `frame_count` whenever the writer ran without vision on a clip that had an `input_path`.
- [x] Add a `--require-vision` flag to `scripts\test_generation.py` for quality validation. When set, raises `VisionRequiredError` for low-context items (generic title + no transcript) if vision was not used.
- [x] Treat generic source titles as low-context (`_is_low_context_source_title`):
  - `Video by meme.ig` / `Reel by <handle>` / `Post by <handle>`
  - filenames that only repeat platform/id metadata (eg. `Instagram_DYfJT5WOtzJ`, `shorts_abc123`)
  - empty or near-empty titles
- [x] For low-context items, retry vision once with `LOW_CONTEXT_RETRY_FRAME_COUNT=2` frames before accepting writer-only output. Smaller prompt has a real chance of clearing Groq's 30K TPM window.
- [x] Add tests for: low-context detection, generation_meta diagnostic fields, `require_vision` raises on low-context vision failure, `require_vision` passes when vision used, `require_vision` passes when context is high even without vision, low-context retry fires with fewer frames, retry is skipped on high-context items.

#### Fix B: rewrite meme caption style away from textbook definitions

- [x] Removed "ground a new viewer (what the thing is)" instruction from the `gaming_meme` / `reaction_clip` profile — that line was the source of the encyclopedia-opener drift. Replaced with "one concrete detail from THIS clip, NOT a definition of the game/show/format."
- [x] Same narrowing applied to the default `contextual_info` caption_style_line.
- [x] Added caption-style-aware AVOID bullets via `_anti_explainer_avoid_lines()`:
  - Ban on encyclopedia openers (`X is a popular Y game where...`)
  - Ban on explain-the-joke openers (`This clip is funny because...`)
  - Ban on echoing the account `target_audience` string verbatim (injects the actual string into the ban when present)
  - Bans skipped automatically for `meme_factual` style (intentionally Wikipedia-tone) so they do not fight the style.
- [x] Added NEGATIVE/POSITIVE EXAMPLES block via `_negative_caption_examples_block()`:
  - Bad: `Minecraft is a popular sandbox game...`
  - Bad: `Gen Z gamers and meme fans are always on the lookout...`
  - Bad: `This clip is funny because the trap finally worked...`
  - Good: `That panic when the trap finally works and nobody knows who to blame.`
  - Good: `Bro really thought he had it figured out.`
  - Good: `POV: you spent two hours building this and it works first try.`
  - Skipped for `meme_factual` and `narrative` styles (would conflict with intent).
- [x] Tests added: removed-wording asserts, anti-explainer present/absent per style, target_audience verbatim injection, end-to-end prompt assembly contains bans + examples, meme_factual prompt skips bans.
- [ ] Re-run on real clips:
  - `.venv\Scripts\python.exe scripts\test_generation.py --account 4 --limit 5 --require-vision`
  - `.venv\Scripts\python.exe scripts\test_generation.py --account 2 --limit 5 --require-vision`
- [ ] Accept only if most items use vision or clearly explain why vision is unavailable, titles are clip-specific, and captions do not start with textbook definitions.

### Crop validation status

- [x] Automated processing tests pass: `tests/test_processing.py` -> `32 passed`.
- [x] Existing saved processed output for `Instagram_DYfJT5WOtzJ` is not good because it still includes the old source title and stale fallback title text.
- [x] Fresh direct export with `suggest_title_replacement_crop` removed the old top title, kept the main video and bottom subtitle, rendered a clean new top-band title, and stayed `1080x1920`.
- [ ] Still needs one app-level Processing run through the PyQt UI to prove the UI path selects the same replacement crop.

### Caption-style enhancements shipped (Fix C bundle)

After Fix A + Fix B landed, three further quality lifts based on real-world
reference posts (meme.ig and @theanomalists):

- **A: Context / Info zoom-in arc.** Split `contextual_info` from the
  generic default in `_caption_paragraph_rule` and `_caption_style_title_rules`.
  Encodes the @theanomalists 3-paragraph template explicitly: P1 hook
  (1 sentence, 8-16 words) → blank → P2 broader context (named entities,
  no encyclopedia openers) → blank → P3 THIS moment (specific clip
  payoff) → blank → 3-5 mixed hashtags.
- **B: News Brief style added end-to-end.** New `news_brief` caption
  style modelled on meme.ig post-1 (Emergent Labs / Y Combinator). Short
  engagement opener line + 2-4 single-fact paragraphs, each ending in
  1-2 semantic topic emojis (💸 funding, ⚡ tech, 🧠 AI, 📈 growth,
  🎬 film, 📺 TV, 🔥 hype, 🏈 sports, 🎤 music). 60-120 words, 0-2
  optional hashtags. Branches added in all per-style helpers; "News
  Brief" dropdown entry added to the Processing panel; Fix B's anti-
  explainer bans and negative examples automatically skipped (this
  style IS factual paragraphs).
- **C: Name-the-thing rule.** New `_name_the_thing_rules(vision_payload)`
  helper that fires across all styles when vision extracted
  `referenced_entity` / `main_subject` / `referenced_concept`. Lists
  the exact names extracted and forbids generic hedging like 'this
  guy', 'an actor', 'a famous show'. No-op when vision found nothing.

Verification: smart_drafts 100 passed, processing+main_window 163 passed.

### Title Style decoupling (Path 2)

After Fix A+B+C+enhancements, a new pickable "Title Style" dropdown was
added to decouple the on-screen title format from the caption style,
based on real reference posts (IGHT setup-punchline meme hooks).

- New `_title_style_rules(title_style)` helper in `smart_drafts.py`:
  - Returns `None` for Auto / empty / None — preserves all prior behavior
    by falling back to `_caption_style_title_rules(caption_style)`.
  - Defines a new `meme_setup_punchline` style: 4-12 words, `When X:` /
    `POV: X:` / `Me X:` framing, REQUIRED trailing colon, video footage
    delivers the punchline. Models the IGHT reference.
  - Delegates to `_caption_style_title_rules` for the other known styles
    so any title format can pair with any caption format.
- Threaded `title_style` parameter through `_smart_draft_prompt`,
  `_build_groq_payload`, `_build_ollama_payload`,
  `_generate_groq_smart_drafts`, `_generate_ollama_smart_drafts`, and the
  entry point `generate_smart_drafts`. All defaults are `None` so calls
  that don't pass `title_style` behave exactly as before.
- UI: new "Title Style" dropdown in Processing panel with `Auto (match
  caption style)` as the default + `Meme Setup → Punchline`,
  `Relatable Hook`, `Observational Hook`, `News Headline`,
  `Conversational Hook`, `Descriptive Hook`. The DB column
  `title_style_preset` already existed and is available for per-item
  persistence in a follow-up.
- 11 new tests covering: Auto/empty/None fallback, meme_setup_punchline
  rule body, delegation for known styles, unknown values fall back,
  end-to-end prompt assembly with title_style, backward-compat
  (title_style=None produces same prompt as omitting the parameter), UI
  dropdown wiring, mixed-style end-to-end (News Brief title + Context/
  Info caption), generate_smart_drafts threading.

Verification: smart_drafts 119 passed, processing+main_window 163 passed.

### Template B: "Them: / Me:" contrast meme titles

Real-world reference posts use a two-line contrast pattern that the original
single-line `meme_setup_punchline` rule could not produce reliably. Extended
the style end-to-end (prompt + renderer):

- `smart_drafts.py`: `_title_style_rules("meme_setup_punchline")` now
  describes **two templates** with explicit calibration examples:
  - **Template A** (existing): `When [X]:` / `POV: [X]` / `Me [X]:`,
    single line, trailing colon.
  - **Template B** (new): `Them: "[quote]" \n\n Me [situation]:` and
    related framings (`Everyone:`, `My friends:`, `My therapist:`,
    `Expectation: / Reality:`). Required literal `\n\n` between the
    two lines so the renderer reserves the paragraph break. Total
    6-16 words across both lines. The user's reference example
    (`Them: "you're so sweet and kind!" \n\n Me when I drive:`) is
    included verbatim as a calibration anchor.
- `video.py` renderer changes (paragraph-break support):
  - `_wrap_overlay_text` now splits on `\n\n` first, wraps each
    paragraph independently via new `_wrap_single_paragraph` helper,
    and rejoins with `\n\n`. Single-paragraph behavior is byte-for-byte
    identical to before.
  - `_fit_title_band` bumps the upper height cap from 320 to 480 when
    `line_count >= 3`, so two-paragraph titles aren't truncated below
    the band. Single/double-line titles still respect the original 320.
  - `_title_band_filter_complex` and the overlay variant preserve
    blank-line positions: `wrapped_lines` becomes a list of
    `(original_index, text)` tuples so y-positions reflect the visual
    gap, while the FFmpeg filter chain only emits `drawtext` for
    non-empty lines (chain index stays contiguous).
- Tests: 1 new prompt-side test (`test_meme_setup_punchline_describes_both_templates`)
  and 6 new renderer tests covering `\n\n` preservation, per-paragraph
  wrapping, single-paragraph backward-compat, empty-paragraph collapsing,
  band-height cap bumping for multi-paragraph, and the FFmpeg filter
  chain skipping blanks while preserving the gap.

Verification: smart_drafts + processing 164 passed, main_window 125 passed.

### After generation quality is fixed

Ordered next work:

1. Remove or disable the YouTube automatic uploader path from the Instagram-first Publish Queue
2. Finish the manual Instagram Publish Queue loop: copy caption, open Reel, mark posted, record metrics
3. Add Instagram manual source intake for Reel/profile/hashtag references
4. Continue tuning title overlay sizing and styling if real outputs reveal problems

## 8. Open Questions / Decision Points

- Should `yt-dlp` be bundled, installed separately, or version-pinned in a controlled way?
- Should cross-account duplicate handling become a warning-only signal later instead of a hard block?
- Which Windows versions must be supported for MVP?
- What account fields are truly needed now versus later?
- Next Instagram profile-pool fix: add an explicit preferred rotation priority of `main`, `alt1`, `alt2`, `alt3`, `alt4`; missing profiles must remain harmless until those accounts exist.

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

Do not spend time on these until the manual Instagram publishing MVP works:

- embeddings / niche scoring
- caption removal automation
- advanced clip editing
- TikTok support
- Instagram upload automation through Meta APIs
- logged-in Instagram scraping or bot automation
- stealth / anti-detection work
- complex scheduler logic
- cloud sync

## 13. Recommended Next Step

Clean up the Publish Queue so it is manual Instagram-first instead of mixed with YouTube uploader automation, then add Instagram manual source intake for Reel/profile/hashtag references.

## 14. UI Upgrade Plan (Post-MVP)

Decided 2026-05-20: keep Python as the single backend and replace only the UI layer, after the Instagram-first MVP ships and is packaged. Do not start UI rewrite work until Milestones 5A/5B are done and a packaged build is validated.

Confirmed 2026-05-24: a future web/multi-device direction remains viable and should be kept open. The current PyQt6 MVP should not be rewritten yet, but new workflow behavior should avoid being trapped in UI code. Prefer plain Python service/module boundaries so a later React frontend can call the same backend logic.

### Phase A - Finish the MVP on PyQt6

No UI-stack change. Complete the manual Instagram Publish Queue and Instagram manual source intake on the current PyQt6 UI, then package and smoke-test.

### Phase B - Rewrite the UI on pywebview + web frontend

- Shell: pywebview - native window via the OS webview, Python stays the main process, no local HTTP server.
- Frontend: Vite + React + Tailwind + shadcn/ui in a new `frontend/` directory. Node is a build-time-only dependency.
- Packaging: Vite build emits static assets, PyInstaller bundles them, pywebview loads them from the bundled path.
- Steps: bridge spike (one screen) -> define a thin Python API/bridge layer over `core`/`db`/`processing`/`scraper` -> rebuild screens one at a time (Accounts, Scraping, Downloads, Processing, Publish Queue) -> retire PyQt6 and delete `main_window.py`.
- Rationale: the bridge layer forces UI logic out of the 389 KB `main_window.py`; web styling is the fastest path to a minimalist look.

Rejected for the immediate desktop-wrapper step: Next.js + Python-over-HTTP (web-server framework features go unused on a local desktop MVP, heavier packaging) and Electron (second runtime, large bundle).

### Phase C - Optional Multi-Device Web App

If the manual publishing workflow proves valuable and phone/tablet access becomes important, evolve from local desktop UI to a real web architecture:

- Frontend: React/TypeScript web UI for review, captions, approvals, Publish Queue, and metrics.
- Backend: Python API over the same service layer used by the desktop app.
- Worker: Python/FFmpeg/yt-dlp/Groq processing stays on a PC or server worker, not in the phone browser.
- Storage: keep SQLite/local files for local-web first; consider Postgres/object storage only when cloud sync or remote access is truly needed.
- Rule: do not start the web rewrite until the current MVP loop is validated; keep current code migration-friendly by moving business logic out of PyQt widgets when touching it.
