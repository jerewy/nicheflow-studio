# Publishing Safety & Automation Backlog

Findings from the 2026-06-10 publishing-safety review (multi-account scale-up).
Planned, not yet executed. Ordered by priority.

> **Execution order lives in `docs/BACKLOG_EXECUTION_PLAN.md`** — prioritized
> work orders (WO-1…WO-10) with what/why/where/how/verify, written for a cold
> executor agent. This file stays the registry of raw findings.

## Remote-posting decision (2026-06-10)

- **Interim (active now):** Instagram native scheduler, manual weekly batch via
  web composer / Business Suite Planner. Meta's servers post; laptop can be off.
  Works at current scale (~30-40 min/week for one account); does not scale to
  10 accounts (~5-7 h/week of clicking).
- **Destination (decided):** **Instagram Graph API publisher** — sanctioned,
  no browser, no selector maintenance, fully automated and laptop-independent.
  Requires Meta developer app + accounts linked to FB Pages + app review for
  the Content Publishing permission. **Deliberately LAST (2026-06-10): build
  when the account network is bigger; everything below ships first.**
- **Rejected:** Playwright driving Meta Business Suite ("poor man's Graph
  API") — same account-correlation cost as the API, but unsanctioned and
  selector-fragile; would be throwaway work.
- **Open question to settle before the Graph API build:** account-correlation
  tolerance — linking all network accounts into one Meta Business portfolio
  tells Meta they are related. Decide grouping (one portfolio vs several) first.

## P0 — fix before scaling to 10 accounts

1. **Failed jobs retry forever (retry hammer).** ✅ Done 2026-06-10.
   A failed scheduled job now gets exactly one delayed retry (10–20 min),
   then status `failed`. A checkpoint fails the job immediately and puts the
   **account** on a 3 h in-process cooldown — `list_due_jobs` hides its jobs
   until the cooldown passes. (`services/publish_now.py`)

2. **No gap between consecutive posts in a batch.** ✅ Done 2026-06-10.
   `publish_due_jobs` sleeps a randomized 2–6 min between consecutive posts
   in a batch. Runs on the background job thread, so the UI stays responsive.

## P1

3. **Move the auto-publish loop into the Python backend** (background thread).
   Today it lives in the React Processing screen and only runs while that
   screen is mounted with the toggle on. Prerequisite for any remote/always-on
   posting setup.

4. **Cross-account slot stagger.** Slot scheduling is per-account; ten accounts
   with "09:00, 18:00" all become due simultaneously.
   Interim: set different slot times per account by hand (works today).
   Later: scheduler avoids assigning two accounts slots within ~15 min.

## Cadence ramp plan — pastmomentsdaily 4 → 5 posts/day (planned 2026-06-10)

Account is ~15+ days old, posting consistently, no checkpoints. Current slots:
`09:00, 13:00, 17:00, 21:00` (4/day, ~4 h apart). Plan to add a fifth slot:

- **Precondition:** ≥7 more days at 4/day with zero checkpoints/flags and
  normal reach (no sudden view collapse, which can signal a soft limit).
- **Change:** add one slot, e.g. `11:00` → `09:00, 11:00, 13:00, 17:00, 21:00`,
  or re-spread to `08:00, 11:30, 15:00, 18:30, 22:00` (~3.5 h apart). Jitter
  (0–15 min) and next-open-slot auto-scheduling need no changes.
- **Watch window:** 7 days at 5/day. One checkpoint → drop back to 4/day for
  two weeks. Reach collapse without a checkpoint → also drop back.
- **Scope:** pastmomentsdaily only; other accounts ramp on their own history.
- **Backlog math note:** at 5/day the distribution backlog target becomes
  5 × 7 = 35 clips if `daily_posts_per_account` is raised to match
  (`core/distribution.py`); only do that once the ramp sticks.

## P2

5. **Single-instance guard.** The publish lock is in-process; two app instances
   could drive two browsers concurrently. File-lock or single-instance check.

6. **Fingerprint/IP correlation — defer proxies.** All accounts share one
   machine/IP/viewport. Behavioral mitigations above are the right spend;
   revisit per-profile proxies only if correlated checkpoints appear.

7. **Distribution default 4 posts/day is aggressive for young accounts.**
   Consider default 2–3 (`DEFAULT_DAILY_POSTS_PER_ACCOUNT` in
   `core/distribution.py`); let aged accounts earn 4.

## P3

8. **Legacy cookie-based scraping path** (instaloader/yt-dlp with session
   cookies in `scraper/instagram.py`) still exists. Sourcing is Apify-only now;
   verify no UI flow reaches the legacy path, then delete it.

9. **Cookies unencrypted under `data/browser-profiles/`.** Acceptable for a
   personal desktop MVP; do not sync that folder to cloud storage.

## Automation & quality roadmap (added 2026-06-10)

Multi-account efficiency items from the 2026-06-10 review. Rough build order:
A → D → B → C (C depends on P1-1 and D; the Graph API publisher stays last).

### A. Auto-distribute upgrades (multi-account efficiency)

1. **Schedule-on-distribute.** Distribute assigns clips to accounts but not to
   the calendar — every export still needs a per-item auto-schedule click.
   When assignments commit, auto-fill each account's open slots across the
   planning window (reuses `next_open_slot_time`; no new scheduling logic).
2. **Close the feedback loop.** `UploadJob` already has
   `posted_views/likes/comments/shares` columns. Feed real per-account
   performance back into `engagement_score` so distribution learns which clip
   styles work on which account, instead of ranking only by source-post likes.
3. **Per-account daily cadence field.** Replace the global
   `DEFAULT_DAILY_POSTS_PER_ACCOUNT` with an `Account` column so a 15-day
   account can run 4–5/day while a 3-day account runs 2/day; backlog targets
   (`daily × window`) then differ per account automatically.
4. **Near-duplicate guard at pool intake.** URL/shortcode dedup exists; add the
   existing visual-dedup pass (`processing/dedup.py`) at intake so two
   different posts of the same underlying clip can't land on two accounts.

### B0. Capture extension UX (do before B — requested 2026-06-10)

The popup auto-closes on focus loss (Chrome platform rule — popups cannot stay
open). Replace it with the **Chrome Side Panel API** (`chrome.sidePanel`) so
the capture UI stays docked while scrolling reels. With the panel in place:

1. **Parallel queueing:** keep "Queue Current" usable while a previous Apify
   batch is processing (queue state independent of batch state).
2. **Editable queue:** list queued links in the panel with per-item remove.
3. Keep the existing pool/estimate/monthly counters visible in the panel.

### B. Capture extension → assisted discovery (semi-auto)

Today: one click per reel, manual. Goal: browse a feed/hashtag and let the
extension passively collect every reel that scrolls by — shortcode, likes,
views, age — score them locally, and surface a ranked "worth pooling" list.

- **Scoring = existing `engagement_score` + like-velocity** (likes ÷ days
  since posted beats raw likes for repost freshness) + niche keyword match.
  Human still reviews the ranked list; Apify still does extraction. No
  ML/vision needed for v1 — metadata gets 80% of the value.
- **Safety boundary (important):** the extension must stay a *passive
  observer* of manual scrolling on a burner/non-network session. Auto-scroll
  bot behavior in a logged-in browser is exactly the scraping risk Apify was
  adopted to avoid — if more volume is needed, raise Apify batch size, don't
  bot the browser.
- Later (v2): optional vision pass (existing Groq Scout path) on the top N
  candidates only, to filter watermarked/text-heavy/low-quality frames.

### C. Hands-off multi-account pipeline (candidate → posted)

End state: per account, the app pulls the next assigned candidate and runs
draft → export → schedule → publish without clicks, governed by daily caps,
checkpoint cooldowns, and the publish lock. Staged rollout, one gate at a time:

1. **Stage 1 — auto-prepare:** auto-draft + auto-export the next N candidates
   per account; human approves before anything is scheduled.
2. **Stage 2 — auto-schedule:** approved exports go straight into open slots
   (builds on A1).
3. **Stage 3 — auto-publish:** requires P1-1 (backend auto-publish loop) and a
   per-account "automation enabled" toggle; manual approval remains the
   default for young accounts.
   Never skip stages: every auto-published reel must have passed through the
   same safety rails (caps, cooldowns, dedup) as a manual one.

### D. Draft generation upgrade (Groq output quality)

Current: Groq free tier — Llama 3.3 70B writer + Llama 4 Scout vision,
~$1/month budget. Quality ceiling is the model, not the prompt plumbing.

Note: Groq IS a free cloud API (not local; Ollama is the local fallback) —
the ceiling is the free model, not the plumbing. Order decided 2026-06-10:

1. **Prompt first (free):** inject 3–5 of the account's best-performing real
   captions as few-shot style examples — biggest lift per effort, works on
   the current Groq model, personalizes per account. (Full version depends
   on A2 performance data; a hand-picked static set works immediately.)
   **Hook findings from the 2026-06-10 competitor review** (redhistory_,
   theanomalists, insidehistory vs pastmomentsdaily insights): our titles
   are factual summaries that resolve the story in the title ("The 1996
   awards moment that made MJ look shy") — competitors open a curiosity
   loop instead. Prompt should require: (a) withhold ONE key detail —
   subject, outcome, or reason ("She performed what would become…");
   (b) reaction-voice openers ("Wait…", "Nobody expected…", "Ain't no
   way…"); (c) at least one of the three options uses a question/direct
   address to bait comments. Own data agrees: top post (17.1K) is the most
   emotional framing; flat factual titles sit in the 500–900 tier.
   Competitor tactic worth copying: repost the same clip with different
   hook titles (A/B) — the 3-option draft output already provides the
   variants.
2. **Swap the WRITER to a cheap strong model:** DeepSeek V3 (~$0.27/M in,
   $1.10/M out) or Kimi K2 — markedly better casual social-media English
   than Llama 3.3 at pennies/month for this volume. They are text-only:
   keep Groq Llama 4 Scout for the vision/frame pass. The code already
   separates vision model from writer model, so this is a clean swap.
3. **If still not enough:** Claude Haiku 4.5 / Gemini Flash as the writer
   for top-tier clips (single-digit $/month even at 10 accounts × 5/day).
4. Keep the paste-parser plain-text contract unchanged regardless of
   provider (see CLAUDE.md draft format rules).

## Already solid (no action)

- Manual login, cookie-only sessions, no stored credentials.
- Headed real Chrome, no fake stealth flags; checkpoint detection pre/post share.
- Slot scheduling with randomized jitter; serialized posting (module lock).
- Apify-only sourcing with monthly usage caps; anonymous downloads.
- Global pool dedup → one asset never posts to two accounts.
