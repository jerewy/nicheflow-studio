# Engagement Feedback Loop — Implementation Plan

Last updated: 2026-06-20
Status: Approved to build. Ordered for runnable checkpoints.
Origin: Analysis of real Instagram insights for the owned accounts (`pastmomentsdaily`,
`beneathhistory`) plus a 726-post study of `@historytrails`. This is the Phase 5
"measure our own results and feed it back" loop referenced in
`docs/SOURCING_POOLING_PLAN.md` §6. Related: `docs/HISTORY_HOOK_STRATEGY_PLAN.md`,
`docs/competitor-learning-findings.md` (read those first; this extends, does not replace).

> Implementer note (Codex): this doc is self-contained. Line numbers are pointers and
> may drift; confirm by symbol name. Anything marked **LOCATE** means find the real
> site before editing. Match repo guardrails: small scoped changes, tests with each
> behavior change, "add tables alongside, don't refactor working paths," lightweight
> compatibility migrations (no Alembic), Windows-only MVP, `.venv\Scripts\python.exe`.

---

## 0. The one idea

Today titles, pool acceptance, and distribution all rank/generate from guesses or from
raw vanity metrics (likes, views). We now have real per-post conversion data. Rewire
four parts to learn from it.

**Hard constraints (do not violate):**
- **No auto-posting and no auto-accept.** Pool acceptance stays a manual approve/reject
  gate (`services/pooling.py::approve_pool_items` / `reject_pool_items`). New scoring only
  **sorts and pre-flags**.
- **Accounts stay broad (campaign model).** Do NOT add per-account subject affinity / do
  NOT narrow an account toward one subject. Keep the existing variety spread in
  distribution. (Consistent with `SOURCING_POOLING_PLAN.md`: broad history accounts, no
  strict sub-niches.)
- **Title flexibility must not regress.** The length control is additive and defaults to
  current behavior; the HistoryTrails voice/shape/grounding rules are untouched.
- **Copy-chat-prompt path and API path must stay in sync.** Both already build from the
  same source (`smart_drafts.effective_title_rules` + `few_shot_winners`, see
  `draft_handoff.py::build_chat_prompt`). Make changes in the shared builder so both
  benefit; never fork them.
- **NOT in scope (deferred, YAGNI):** external hook-psychology research; a separate
  save/share/comment "hook intent" system. Revisit only if metrics later show a gap.

---

## 1. Data findings that justify the changes (ground truth)

Pulled via the Graph API (`graph.instagram.com` v21.0) using the existing
`IG_TOKEN_<ACCT>` / `IG_USER_ID_<ACCT>` secrets in `.env`. Reference CSVs already exist:
- `data/ig_insights_pastmomentsdaily.csv` (116 reels, full saves/shares)
- `data/ig_insights_beneathhistory.csv` (30 reels)
- `data/title_analysis/historytrails-full/titles.csv` (726 posts, view/like/comment + ER)
- `data/title_analysis/historytrails-ocr/title_template_style.md` (hook style DNA)
- Throwaway puller to formalize: `data/ig_insights_pull.py`

Findings:
1. **Reach is solved; conversion is the gap.** Reels reach far past follower count, but
   follower growth is low. Optimize saves/shares per reach, not views.
2. **Raw views/likes lie ("junk reach").** Confirmed on all 4 accounts: high-view, low-ER
   posts convert nothing (e.g. HistoryTrails hydraulic-press clip: 9.4M views, 1.4% ER;
   beneathhistory Deadpool: 16k reach, 7 saves, 1.4% ER). Rank by **engagement rate /
   composite**, never raw likes.
3. **What converts:** iconic music/performance + emotion + nostalgia + a recognizable
   subject WITH a story beat. Recognizability alone buys reach, not saves.
4. **HistoryTrails hook formula** (their proven engine, the style we imitate): long
   documentary sentence, ~15-25 words, 4 overlay lines, named person/year/place,
   **withheld payoff (open loop)**. Top format: "That time <person> <improbable thing>".
5. **Long wins on HistoryTrails partly because of audience size.** Our own accounts must
   verify length on their own data → hence the length control + ability to A/B later.

---

## 2. Composite conversion score (define once, reuse everywhere)

Use a single helper so titles, acceptance, and distribution agree.

**Owned accounts (full insights available):**
```
conversion_score = (3*saved + 3*shares + 2*comments + 1*likes) / max(reach, 1)
```
Weights: saves/shares predict follows + spread (highest), comments feed the algorithm and
community (mid), likes are cheap (lowest). Keep all raw metrics too; the score is for
ranking, the columns are for inspection.

**Source candidates (only public metrics: views, likes, comments):**
```
source_er = (likes + comments) / max(views, 1)
```
This is the rank signal for pool acceptance/distribution, since competitors' saves/shares
are private.

---

## 3. Topic tiers (store in `PoolItem.topic_tag`)

Lightweight keyword map first (KISS; AI topic tagging is deferred per
`SOURCING_POOLING_PLAN.md` Phase 6). Classify candidate title + caption text.

| Tier | Weight | Definition | Seed keywords |
|---|---|---|---|
| S | 1.6 | Iconic music/performance with emotion/nostalgia; deeply emotional human moment with payoff | performance, sang, song, concert, stage, tribute, VMA, duet, grief, loss, reunion, "wrote for", "last time", "decades later", returned |
| A | 1.3 | Beloved-IP nostalgia; recognizable person + a surprising story beat | cartoon, childhood, theme song, classic, remember, "that time", "for the first time" |
| B | 1.0 | Recognizable event/person + concrete reveal, lower emotional charge | sports, match, record, behind the scenes |
| C | 0.5 | Famous face, no story beat: appearance/spotted/confidence/reaction, somber-passive | spotted, appearance, "walked on stage", confidence, reaction, "funeral procession", photographed |
| D | reject | Dry novelty/trivia, no emotional/nostalgic hook; or no recognizable subject and no payoff | hydraulic press, physics demo, oscilloscope, raw trivia |

Acceptance suggestion logic (suggest only; human decides):
- Suggest ACCEPT: tier S/A/B AND passes hard gates AND `source_er >= 0.03`.
- Manual review: tier B with weak signal, or S/A with low `source_er`.
- Suggest REJECT: tier C/D or any failed hard gate.

Hard gates (keep existing, in `SOURCING_POOLING_PLAN.md` "History pool acceptance rules"):
payoff in 1-2s; duration <= 35s (ideal 20-30s); supports a 15-25 word specific +
withheld-result hook (a vague mood line is tier D); recognizable subject.

---

## 4. Work items

### WI-0 — Insights puller + metrics store (foundation)
**Goal:** a repeatable tool that stores per-reel metrics for owned accounts so the rest of
the loop can read measured winners.
- Promote `data/ig_insights_pull.py` to `scripts/ig_insights.py`. Use
  `nicheflow_studio.core.env.load_dotenv` (not a hand-rolled parser). Read
  `IG_TOKEN_<ACCT>` / `IG_USER_ID_<ACCT>`. Never print the token; redact error bodies.
- Pull `me` + `/{user_id}/media` (paginate) + `/{media_id}/insights`
  (metric fallback chain: `reach,likes,comments,saved,shares,total_interactions,views`
  → drop `views` → drop `shares,total_interactions` on 400). Host `graph.instagram.com`,
  version `v21.0` (match `scripts/cloudflare_register_account.py`).
- New additive table `account_post_metrics` (**LOCATE** `db/models.py`, follow the
  MediaAsset/PoolItem additive pattern; lightweight `ALTER`/create in `db/session.py`):
  columns `account_key`, `shortcode`, `caption`, `timestamp`, `reach`, `views`, `likes`,
  `comments`, `saved`, `shares`, `total_interactions`, `conversion_score`, `pulled_at`.
  Upsert by `(account_key, shortcode)`.
- Compute `conversion_score` per §2. Also write the existing CSV for eyeballing.
- **Acceptance:** `.venv\Scripts\python.exe scripts\ig_insights.py pastmomentsdaily`
  upserts rows for all 116 reels with `conversion_score` populated; rerun is idempotent.
- **Tests:** `tests/test_ig_insights.py` — composite formula; upsert idempotency; metric
  fallback parsing. Mock HTTP; no live calls in tests.

### WI-1a — Title length control (additive)
**Goal:** Short / Medium / Long / Auto-mix, defaulting to current behavior.
- Thread a new `title_length: str | None` param alongside `title_style` through
  `smart_drafts.generate_smart_drafts` and the internal chain (the same call sites that
  pass `title_style`: ~178, 343, 363, 397, 424, 513, 551, 650). Default `None` => "long".
- Length bands injected into the prompt (extend `_historytrails_title_rules`, and add a
  shared `_title_length_rules(title_length)` helper used by `effective_title_rules`):
  - short: 5-9 words, 1-2 overlay lines (only when the clip is self-explanatory)
  - medium: 10-16 words, 2-3 lines
  - long: 15-28 words, 4 lines (current HistoryTrails text, unchanged)
  - auto: the 3 generated options must span short/medium/long
- UI: add a "Title Length" dropdown next to Caption/Title/Template (**LOCATE** the
  workflow-settings panel in `frontend/src/`; values short|medium|long|auto, default
  long) and persist it with the other workflow settings (**LOCATE** the
  "Save workflow settings" handler + settings schema in `frontend/src/types.ts` /
  `lib/bridge.ts` and the Python settings sink). Pass it into both the API path and
  `draft_handoff.build_chat_prompt` (it already receives `settings`).
- **Acceptance:** Long produces output identical to today (regression-guarded). Short
  yields <=9-word titles. Auto yields a visible short/medium/long mix across the 3
  options. The copy-chat-prompt text reflects the selected length.
- **Tests:** `tests/test_smart_drafts.py` — each mode injects the right band; default ==
  long; auto spans lengths; chat prompt and API prompt contain the same length rule.

### WI-1b — Measured winners few-shot + title-clip grounding (WO-6)
**Goal:** the title AI imitates the account's real top titles, and titles stop
mismatching the clip.
- Add a resolver (e.g. `db/post_metrics.py::top_titles_for_account(account_key, n=5)`)
  returning the highest-`conversion_score` titles from `account_post_metrics`.
- Feed them as `few_shot_winners` into `effective_title_rules` for both the API path and
  `draft_handoff.build_chat_prompt` / `_account_prompt_header` (they already accept
  `few_shot_winners`). Fall back to the existing static
  `_HISTORY_LOST_ARCHIVE_FEW_SHOT_WINNERS` / `_HISTORYTRAILS_FEW_SHOT_WINNERS` when no
  metrics exist for that account. This is exactly the swap the line ~50-54 comment
  ("WO-6 replaces this static block with measured per-account winners") anticipates.
- Grounding / title-clip match: ensure title generation runs the vision/green-tier path
  (`require_vision`, `claim_supports`, the "STAY GREEN-TIER" rules already present). For
  the copy-paste flow, keep using `draft_handoff.batch_frames` (exports one still per reel
  for the user to attach). Document the local-vision-model option (run a local multimodal
  model to verify title-vs-frame match offline) as a future enhancement, not this WI.
- **Acceptance:** with metrics present, the prompt's "MEASURED ACCOUNT WINNER EXAMPLES"
  block shows that account's real top titles; with none, it falls back to static without
  error. Caption parser and paste format unchanged (see project CLAUDE.md "Draft Output
  Format" rules — do not alter section headers).
- **Tests:** resolver returns top-N by score; fallback path; prompt contains measured
  block when available.

### WI-2 — Acceptance ranking (manual gate unchanged)
**Goal:** surface save-worthy clips first; stop ranking by raw likes.
- Change the pool fit score: `assignments_db._engagement_scores_for_pool_items` (used by
  `services/pooling.py::review_queue`, line ~238) to rank by `source_er` (§2) times the
  topic `tier_weight` (§3), with the recency term kept. Remove raw-likes dominance.
- Classify candidate text into a tier and persist to `PoolItem.topic_tag`
  (field already exists per `SOURCING_POOLING_PLAN.md` Phase 2). Add the tier + a
  suggested action ("accept"/"review"/"reject") to the `review_queue` row dict so the UI
  can show it.
- Keep `approve_pool_items` / `reject_pool_items` exactly as-is. No auto state changes.
- **Acceptance:** in the review queue, a high-view/low-ER clip ranks below a
  lower-view/high-ER clip; tier + suggestion appear per row; nothing is auto-accepted.
- **Tests:** `tests/test_pooling_service.py` / `tests/test_assignments.py` — composite
  ranking order; tier classification; existing approval tests still pass.

### WI-3 — Distribution ranking (keep variety)
**Goal:** push higher-converting clips first, accounts stay broad.
- In `assignments_db.distribute_niche` (driven from `services/pooling.py::distribute_niche`
  line 479), replace the "likes + recency" rank with `tier_weight * source_er` (+ recency).
- Keep the existing jitter/variety spread so accounts don't all get the same clip and stay
  topically broad. **Do NOT add per-account subject affinity.**
- **Acceptance:** distribution prefers high-tier/high-ER clips; per-account volume balance
  and variety spread unchanged; existing `tests/test_distribution.py` /
  `tests/test_assignments.py` still pass.

### WI-4 — Scraping inputs
**Goal:** better-quality candidates, no Groq quota failures.
- Add `historytrails` to the history source preset (**LOCATE** the preset: referenced in
  `SOURCING_POOLING_PLAN.md` "History source preset" and the Source Intake "Add History
  Pool" preset in `frontend`/`app`). Re-verify the previously dead handles.
- Compute and persist `engagement_rate` on `ScrapeCandidate` at ingest time
  (`scraper/instagram_apify.py` ingest path → `db/models.py::ScrapeCandidate`), using
  `source_er` (§2). `historytrails-full/titles.csv` already has the column as a reference
  shape.
- Make the local OCR title extractor the default on-screen-title path (the Groq path in
  `data/title_analysis/historytrails-ocr/titles.csv` is full of HTTP 429 "tokens per day"
  errors). **LOCATE** the OCR entry point (related: `scripts/extract_history_titles.py`,
  the HistoryTrails title pipeline) and switch the default away from Groq.
- **Acceptance:** new scrapes populate `engagement_rate`; `historytrails` is in the
  preset; title extraction runs locally with no Groq call by default.
- **Tests:** ingest computes ER; preset contains the handle.

---

## 5. Build order
1. **WI-0** insights puller + `account_post_metrics` (foundation; data already pulled once).
2. **WI-1a + WI-1b** titles (biggest pain + lift, isolated in `smart_drafts.py` +
   `draft_handoff.py`). First PR.
3. **WI-2 + WI-3** acceptance + distribution ranking (share the composite + tiers).
4. **WI-4** scraping inputs.

## 6. Verification
- Run all touched test modules with `.venv\Scripts\python.exe -m pytest`.
- Exercise WI-0/2/3 against a **copy** of `data/nicheflow.db` (see `data/backups/`), never
  the live DB. Nothing in this plan posts to Instagram.
- For WI-1, generate drafts for a known clip in each length mode and confirm Long matches
  pre-change output (regression) and the chat prompt mirrors the API prompt.
