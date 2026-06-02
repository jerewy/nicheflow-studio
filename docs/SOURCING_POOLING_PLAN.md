# NicheFlow Studio — Sourcing, Pooling & Multi-Account Distribution Build Plan

Last updated: 2026-06-01
Status: Approved to build (architecture green-lit with the tweaks below)
Origin: Obsidian `Je Personal Vaults` notes 1 (Multi accounts) and 3 (Scraping pooling), reconciled against the actual codebase.

> This document is the single source of truth for the Apify-sourced, shared-pool,
> multi-account distribution workstream. It supersedes the conflicting scope
> statements in older docs (see §1). `PLAN.md` remains the broader roadmap;
> this file owns the sourcing/pooling slice.

---

## 1. Scope reconciliation (read this first)

Three docs told three different stories. The reconciled truth:

| Source | Said | Reality (2026-05-31) |
|---|---|---|
| `CLAUDE.md` (project) | "Windows-only YouTube via `yt-dlp`" | **Stale.** Target is **Instagram**. |
| `PLAN.md` | 5 dual-use accounts (account = publisher *and* scraper), YouTube intake | Partially stale. Direction is now Apify-sourced shared pools. |
| `STATUS.md` (2026-04-16) | Processing-hardening focus, YouTube | Stale; Instagram auto-publish + session health shipped after it. |
| Je plan (notes 1 & 3) | 10 history + 3 movie accounts, Apify-sourced, shared niche pools | Partially superseded. Current direction is broad random-past/history accounts first; movie is deferred. |

**Confirmed direction:** Instagram is the publishing target. **Apify** is the sourcing
engine (your own IG accounts never scrape — Apify runs on its own infra). YouTube/`yt-dlp`
stays only as a legacy/secondary intake path, not the center of gravity.

**2026-06-01 adjustment:** build the first network as broad random-past/history
accounts, all fed from the shared `history` pool. Do not split into strict
sub-niches yet. Movie/cinema stays available in the schema but is not the first
operational target.

**Key strategy change from `PLAN.md` §2A:** the Je plan abandons the "dual-use account"
idea (where each publisher account also scrapes). Apify sourcing makes dual-use
unnecessary and removes the per-account ban risk `PLAN.md` flagged. Accounts become
**publish-only distribution channels** fed from a shared, niche-separated pool.

---

## 2. Accepted risks (explicit, signed off before building)

These are **business risks the user has accepted**, not bugs to engineer around.
Written down so they are a conscious decision, not a surprise later.

### 2.1 Platform / originality risk — ACCEPTED

The core loop is: scrape a proven source creator's reels → redistribute the footage
across 10+ of our own accounts → regenerate titles/captions. This is the
**content-aggregator pattern Instagram's originality guidelines penalize.** Regenerated
captions change the *post*, not the *originality of the underlying video*.

- Instagram may throttle reach or remove recommendations for content it judges
  unoriginal, and repeat/aggregated content can be made ineligible for recommendation.
- The 14-day reuse cooldown spaces *our own* reuse; it does **not** make borrowed
  footage original.
- **Accepted consequence:** some or all of these accounts may be reach-limited or
  banned. The network is treated as disposable/replaceable, not as durable IP.

### 2.2 Rights / copyright risk — ACCEPTED for MVP

Archival "history" clips and especially **movie scenes** (commercial films) often have
real owners. The plan defers rights verification.

- **Accepted consequence:** takedown/strike exposure in the interim. Movie accounts
  are explicitly **secondary / experimental** and must not be the only future
  monetization inventory (Je note 3 §15.3).
- Mitigation deferred to "later" per §13.4; not a launch blocker.

### 2.3 Account-footprint risk — NOTED, mitigate cheaply

10+ accounts, similar content, likely shared machine/IP/login tooling. The real
detection surface is the **network/session footprint**, not video pixels — which is
why the plan correctly refuses pixel-shuffling (§14). Existing multi-profile login +
weekly re-login (`core/instagram_profile_pool.py`) is the lever that matters here.

---

## 3. Verified technical finding — Apify pagination

**Question:** can we backfill more than the newest N posts (e.g. posts 1851–3700) from
one source account?

**Verified answer (2026-05-31, against the live `apify/instagram-scraper` input schema):
NO.** The actor scrapes **newest → oldest only**. There is **no `offset`, `skip`,
`startFrom`, or cursor** parameter. `resultsLimit` always counts from the most recent
post backward; `onlyPostsNewerThan` only *stops early* at a cutoff. (Sources below.)

**Consequences for the runway math:**

1. **First backfill is fine:** set `resultsLimit ≈ 1800` and the actor pages
   newest→oldest in one run. The existing `scrape_instagram_source_apify(... max_items=1800)`
   already maps `max_items → resultsLimit`, so no code change is needed for the backfill itself.
2. **You cannot later reach deeper backlog** on the same account. Once you've pulled the
   newest ~1800, the *only* ways to grow inventory are:
   - `onlyPostsNewerThan` to pick up **genuinely new** posts the source publishes later
     (already wired via the `since=` parameter), or
   - **add another source account**, or
   - **single-URL imports** of clips found manually.
3. **Reliability caveat:** a single `resultsLimit=1800` run against one profile can return
   *partial* results (IG pagination flakiness on large runs). Budget for **1.5–2× the
   nominal Apify cost** to cover re-runs, and treat "1800 in one run" as a target, not a
   guarantee. Verify the actual returned count per run (`ScrapeRun.items_fetched` already
   records this).

**Decision:** the Je plan's §5.2 "known limitation" is **correct as written**. Do not
design around deep-backlog pagination. Plan inventory growth around new-posts +
new-sources + single-URL imports.

Sources:
- [Instagram Scraper — input schema (Apify)](https://apify.com/apify/instagram-scraper/input-schema)
- [Instagram Post Scraper (Apify)](https://apify.com/apify/instagram-post-scraper)
- [onlyPostsNewerThan issue thread (Apify)](https://apify.com/apify/instagram-scraper/issues/problem-on-onlyposts-JNsmloUrVxdN5do0R)

---

## 4. What already exists (do NOT rebuild)

Grounded in `src/nicheflow_studio/`:

| Capability | Where | Notes |
|---|---|---|
| Apify bulk-profile scrape | `scraper/instagram_apify.py::scrape_instagram_source_apify` | `resultsLimit` + `onlyPostsNewerThan` already wired |
| Apify single-URL import | `scraper/instagram_apify.py::scrape_instagram_urls_apify` | 1:1 URL → candidate |
| Candidate persistence | `db/models.py::ScrapeCandidate` | **account-scoped** (`account_id` required) |
| Source + run tracking | `db/models.py::Source`, `ScrapeRun` | per-account; `items_fetched/accepted/...` |
| Download flow | `db/models.py::DownloadItem`, `queue.py` | `account_id` nullable; `video_id` present |
| Title/caption generation | `processing/smart_drafts.py` | niche-aware (history/movie/meme/cinema profiles) |
| Manual publish queue | `db/models.py::UploadJob`, `publisher/` | posted metrics on the row |
| Multi-profile login + health | `core/instagram_profile_pool.py`, `core/account_health.py` | weekly re-login cadence |

**The scraping engine is essentially done.** The gap is the **data model**, not the scrape.

---

## 5. The core architectural gap

Current model is **account-scoped**: every candidate belongs to one account, dedup is
per-account (`PLAN.md` §"account-scoped duplicate handling"). The Je plan needs the
**opposite**: account-agnostic **shared niche pools** + a **global media library** with
**global dedup** and controlled **cross-account reuse**.

Je plan's target shape (note 3 §6, §16):

```
GLOBAL_MEDIA_LIBRARY   (one physical file per source video, deduped by shortcode/URL)
   └─ pool_items       (niche membership: history | movie, accepted/evergreen flags)
         └─ assignments (pool_item → destination account, with reuse_iteration + cooldown)
               └─ performance_metrics
```

**Decision (tweak vs Je plan): add the pool layer ALONGSIDE the existing tables, do not
refactor the account-scoped path away.** Rationale: matches repo guardrail "add
abstractions only when a second real use case exists," keeps the working YouTube path
intact, and lets us route only the Apify/Instagram candidates into the pooled model.

### 5.1 New tables (additive migration)

- **`media_assets`** — global, deduped original downloads. Key: `canonical_source_url` +
  `instagram_shortcode` (+ `platform_media_id` when present). `download_status`.
  *This is the dedup gate that saves Apify/storage cost.*
- **`pool_items`** — links a `media_asset` to a niche pool. `niche: Literal["history","movie"]`,
  `acceptance_status`, `is_evergreen_candidate`. (Partially overlaps `ScrapeCandidate.state`;
  pool_items is the *accepted, account-agnostic* layer.)
- **`assignments`** — `pool_item → destination_account`, `scheduled_date`, `reuse_iteration`,
  `title_text`, `caption_text`, `render_output_path`. (Extends what `UploadJob` does today;
  may be implemented as new columns on `UploadJob` rather than a new table — see §6 open
  decision.)

### 5.2 New/changed columns

- **`niche` enum** becomes first-class on candidates, assets, pool_items, and accounts.
  Today `Account.niche_label` is free text only — keep it for display, add a strict
  `niche` for the isolation rule.
- **Niche isolation guard** (Je note 3 §7): reject `pool_item.niche != account.niche` at
  assignment time. Empty pool never borrows cross-niche.
- **Global dedup-before-download** (Je note 3 §8): check `media_assets` by
  shortcode/URL before any `yt-dlp`/download; link instead of re-downloading.

---

## 6. Build phases (mapped to real files)

Ordered for runnable checkpoints. Each phase ends green-tested before the next.

### Phase 0 — Doc + scope reconciliation  ✅ (this document)
- Write down accepted risks (§2) and the pagination finding (§3). **Done.**
- Correct stale scope in `CLAUDE.md` / point `PLAN.md` and `STATUS.md` at this file.

### Phase 1 — Niche field + global media library
- ✅ Add strict `niche` to `Account` (+ backfill existing rows from `niche_label`
  via `core/niche.py::classify_niche`; compat `ALTER` + `_backfill_account_niche`
  in `db/session.py`). Verified on a copy of the real DB: Past Moments→history,
  Cinema Files→movie.
- ✅ New `media_assets` table (`db/models.py::MediaAsset`) + dedup helpers in
  `db/media_library.py` (`find_media_asset`, `find_or_register_media_asset`,
  `mark_media_asset_downloaded`, shortcode/URL normalization).
- ✅ Tests: `tests/test_media_library.py` — dedup returns existing asset; same
  shortcode from a different URL dedupes; backfill only fills NULLs (never
  overwrites an explicit niche).
- ✅ **Step 2:** `queue.py` now registers a `MediaAsset` on every successful
  Instagram download (`_register_downloaded_media_asset`, idempotent by
  shortcode/URL; YouTube stays out of the pantry). Re-downloading the same reel
  reuses the one asset. Tests in `tests/test_queue.py` (3 new). *Note: the
  pre-download "skip re-download if the original is already on disk" optimization
  is deferred (title/stale-file edge cases) — registration alone already enables
  pooling and keeps the pantry deduped.*

### Phase 2 — Niche pools + accepted layer
- ✅ New `pool_items` table (`db/models.py::PoolItem`) linking a `MediaAsset`
  into HISTORY_ACCEPTED / MOVIE_ACCEPTED, with `acceptance_status`,
  `is_evergreen_candidate`, `topic_tag`, `rights_confidence`.
- ✅ `db/pools.py`: `accept_into_pool` (idempotent within a niche; **cross-niche
  accept raises `CrossNicheError`** unless explicitly overridden — the isolation
  rule), `pool_items_for_niche`, `pool_size`.
- ✅ Tests: `tests/test_pools.py` (7) — accept creates item, idempotency,
  cross-niche blocked + override path, invalid niche rejected, niche isolation.
- ⬜ **Remaining (Step 2):** candidate-review UI gains a niche tag + an
  "Accept into pool" action (reuse the candidate widgets in `app/main_window.py`).

### Phase 3 — Destination accounts + assignment
- ✅ Distribution algorithm `core/distribution.py::plan_first_cycle` (pure):
  each clip → exactly one account, **round-robin-within-shuffle** so per-account
  volume differs by ≤1, optional `max_per_account` cap, deterministic under a
  seeded rng. `distribution_counts` for a pre-commit preview.
- ✅ Tests: `tests/test_distribution.py` (7) — each clip once, balanced volume,
  ≤1 spread on uneven splits, cap leaves remainder unassigned, determinism.
- ✅ **Persistence (Option A — separate `assignments` table).** New
  `db/models.py::Assignment` (the "order ticket": pool_item → account, `niche`,
  `status`, `scheduled_date`, `reuse_iteration`, nullable `upload_job_id` linked
  at render). Kept separate from `UploadJob` so the publish queue stays clean —
  an assigned clip isn't rendered yet. `db/assignments.py::distribute_niche`
  gathers same-niche accounts + still-unassigned accepted pool items, runs
  `plan_first_cycle`, and writes assignments; safe to re-run (skips
  already-assigned clips, never double-books). Plus `assignment_counts_by_account`,
  `assignments_for_account`.
- ✅ Tests: `tests/test_assignments.py` (6) — every clip assigned once + balanced,
  niche isolation (history run never touches movie), re-run places only new clips,
  no accounts → empty, `max_per_account` cap, per-account lookup.
- **Niche isolation** holds by construction: only `Account.niche == niche` and
  `pool_items_for_niche(niche)` are passed to the planner; `accept_into_pool`
  already blocks cross-niche pool membership upstream.

### UAT harness — `scripts/pool_admin.py`
Drives the whole backend loop on real data before the in-app buttons exist:
`status` · `downloaded` · `backfill` (register MediaAssets for pre-existing IG
downloads) · `accept --niche <n> [--all|--item-id N]` · `distribute --niche <n>
[--max-per-account N]`. Verified end-to-end on a copy of the real DB: backfill
102 → accept into history pool → distribute. Nothing posts to Instagram.

**In-app UI:** the Publishing Dashboard has Pool & Distribute controls for
backfill, accept downloaded clips into a niche pool, and distribute. Source Intake
also has an "Add History Pool" preset to seed the current Instagram account with
starter competitor/reference profiles.

### History source preset

The starter source list (verified 2026-06-01 — see
`docs/competitor-learning-findings.md`):

- `theanomalists` (237K)
- `crazyfactscorner` (128K)
- `thehistologian` (120K)
- `houseofhistorian` (110K)
- `factsontheway` (37.4K)
- `thelegendarist` (smaller; corrected from `thelegendartist`)

Dropped as invalid (empty/nonexistent/mistyped on the 2026-06-01 scrape):
`thelegendartist` (a personal art account), `themysterist`, `thecinemast`,
`entertainist`, `thelegendast`. Replace with correct handles if found.

These are source-intake references only. They do not auto-download or auto-post.
The intended loop remains: scrape candidates, review/filter against the acceptance
rules below, download useful clips, accept downloaded clips into `history`, then
distribute.

### History pool acceptance rules (learned 2026-06-01)

Locked from competitor analysis of 180 posts across the 6 verified accounts (full
write-up: `docs/competitor-learning-findings.md`). Step 2 candidate-review /
`accept_into_pool` should apply these for the `history` niche:

1. **Recognizable subject required** — known person, movie, event, or IP (the
   strongest performance signal).
2. **Supports a 15–22 word story hook** (observed median 20 words — revised up from
   the earlier 10–16 assumption).
3. **Clip ≤ ~35s**, ideally ~20–30s.
4. **Understandable in 1–2 seconds**; must have a payoff (no footage that goes nowhere).
5. **Plan for a long caption (~150 words)**, not just the on-screen hook.
6. **Broad pop-history categories**: history, movie/TV, celebrity, sports, music,
   weird/mystery, old TV, internet history.
7. **Avoid excessive baked-in text** unless it carries the story.

Revisit after we post and measure our own results (Phase 5 loop).

### Phase 4 — Draft + render per assignment
- Generate per-account title/caption via existing `smart_drafts.py` niche profiles.
- Render account-specific output; track `render_output_path`. Reuse current Processing/export.
- Feeds the existing manual Publish Queue (`UploadJob`/`publisher/`).

### Phase 5 — Reuse + performance loop
- `MIN_REUSE_GAP_DAYS = 14` (configurable), `reuse_iteration`, reuse limits
  (untested 1 / strong 2 / evergreen 3 — Je note 3 §13).
- Performance metrics already partly on `UploadJob`; surface winners for reuse.

### Phase 6 — Deferred (do not build yet)
Perceptual visual dedup across different source URLs, AI topic tags, rights-confidence,
analytics dashboards. (Je note 3 §17 "Not Required Yet".)

---

## 7. Open decisions to confirm before Phase 1 code

0. **2026-06-01 strategy update:** build history-style accounts first, all drawing
   from the shared `history` pool. Movie is deferred.

1. **Account count / strategy:** adopt Je plan's **10 history + 3 movie**, replacing
   `PLAN.md` §2A's "5 dual-use" portfolio? (Recommended: yes — Apify sourcing makes
   dual-use obsolete.)
2. **`assignments`: new table vs extend `UploadJob`?** `UploadJob` already does most of
   it. Leaning **extend `UploadJob`** (add `pool_item_id`, `reuse_iteration`) to avoid a
   parallel structure — confirm.
3. **Movie network now or later?** History is primary; movie is secondary/experimental.
   Build history end-to-end first, add movie tables but defer movie accounts?
4. **Migration style:** repo uses "lightweight compatibility upgrades," not Alembic.
   Keep that pattern for the new tables? (Recommended: yes, stay consistent.)

---

## 8. First-launch operational runway (with §3 reality baked in)

- 1 source account → one `resultsLimit≈1800` Apify run → ~312 already on hand + new pulls.
- Download **only accepted** candidates (Je note 3 §10) — keeps Apify/storage cost down.
- 10 history accounts × ~30 first-cycle posts = ~300 unique assignments before reuse.
- Inventory growth after backfill = new-posts (`onlyPostsNewerThan`) + new sources +
  single-URL imports. **Not** deep-backlog pagination (impossible — §3).
- Budget **1.5–2×** nominal Apify spend for partial-run re-tries.
