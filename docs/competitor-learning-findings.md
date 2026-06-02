# Competitor Learning Findings — History/Facts Reels

**Date:** 2026-06-01
**Method:** Scraped the 30 most-recent posts from each reference account via Apify
(`apify/instagram-scraper`, reused `scraper/instagram_apify.py::scrape_instagram_source_apify`).
Raw data + computed metrics under `data/_competitor_learning/<run-timestamp>/`
(gitignored). Script: `scripts/competitor_learning_scrape.py`.

This is the durable reference behind the `history` pool acceptance rules in
`SOURCING_POOLING_PLAN.md`.

---

## 1. Reference set (verified)

10 handles were proposed; **6 are valid faceless history/facts pages**. The other 4
returned empty/garbage (don't exist, private, or mistyped) and were dropped.

| Handle | Followers | Posts | Status |
|---|---|---|---|
| theanomalists | 237K | 1,667 | ✅ reference |
| crazyfactscorner | 128K | 1,307 | ✅ reference |
| thehistologian | 120K | 1,065 | ✅ reference |
| houseofhistorian | 110K | 1,314 | ✅ reference |
| factsontheway | 37.4K | 656 | ✅ reference |
| thelegendarist | — (smaller) | — | ✅ reference (corrected handle) |
| ~~thelegendartist~~ | — | — | ❌ personal art account, not the page |
| ~~themysterist / thecinemast / entertainist / thelegendast~~ | — | — | ❌ empty / nonexistent |

Follower/post counts are from the profile pages; per-post medians below are from
the 180 scraped posts (30 × 6).

---

## 2. Per-account scraped metrics (30 recent posts each)

| Handle | Hook words (median) | Duration s (median) | Caption words (median) | Views (median) | Views (max) |
|---|---|---|---|---|---|
| theanomalists | 20 | 21 | 151 | 6,837 | 280,138 |
| thehistologian | 20 | 32 | 146 | 3,942 | 82,480 |
| crazyfactscorner | 19 | 28 | 141 | 3,705 | 52,766 |
| houseofhistorian | 20 | 32 | 160 | 3,374 | 50,947 |
| factsontheway | 20 | 32 | 157 | 1,612 | 29,018 |
| thelegendarist | 19–20 | 31 | 141 | 1,363 | 17,515 |
| **All 180 posts** | **20 (range 12–24)** | **~29 (range 6–35)** | **~150** | — | — |

The formula is remarkably consistent across accounts of very different size.

---

## 3. The formula (how they post everything)

### Profile / brand
- **PFP:** a stylized *illustrated portrait of one iconic figure* on a flat dark
  background (detective silhouette, Dalí, JFK, Washington, Chaplin). Consistent
  "illustrated icon" aesthetic.
- **Bio template:** `Brand name` → niche tags pipe-separated
  (`History | Facts | Entertainment`) → a **reverse-psychology CTA**:
  *"Do NOT follow if your brain hates surprises" / "…if you're not into CRAZY
  stories" / "…if you fit into the masses."* Category set to "Community".
- **Handle pattern:** scholarly-sounding `the[noun]ian/ist` (anomalists,
  histologian, historian, legendarist) or `[adjective]facts`.
- **Scale & cadence:** 37K–237K followers, 650–1,670 posts each — a high-volume
  repost engine.

### Post mechanics
- **On-screen hook text at the top** (white) over a clip below, usually
  letterboxed in a black canvas; frequently a baked-in subtitle too.
- **Hook = one complete story sentence, median 20 words (range 12–24).** Not short
  punchy lines — story-style setups.
- **Duration: median ~29s (range 6–35);** high-performers skew slightly shorter (~22s).
- **Every post also has a long written caption (~150 words median)** — they use
  *both* on-screen hook *and* a full caption.

### Topics
Broad pop-history, not tight themes: **history-dominant, then movie/TV**, then
sports / music, with celebrity / crime / weird sprinkled in. Matches the bios'
"History | Facts | Entertainment" positioning.

### What the high-performers share
Of the posts above 1.5× their account's median views, the biggest winners are
**movie/TV history, nostalgia, and recognizable people/IP** with a clear story
beat — psychological thrillers, Pirates/Lion King/Guardians, Princess Diana,
famous cartoon characters, a viral golf moment. **Recognizability + a clear story
beat = the winners.** High-performers were a touch shorter than average.

---

## 4. Locked acceptance rules (for the `history` pool)

Derived from the above; mirrored in `SOURCING_POOLING_PLAN.md`.

1. **Recognizable subject required** — a known person, movie, event, or IP. This is
   the strongest performance signal.
2. **Supports a 15–22 word story hook** (their actual median is 20 words — *not*
   the 10–16 originally assumed).
3. **Clip ≤ ~35s**, ideally ~20–30s.
4. **Understandable in 1–2 seconds** of watching.
5. **Has a payoff** — avoid clips whose footage goes nowhere.
6. **Plan for a long caption (~150 words)**, not just the on-screen hook.
7. **Broad pop-history categories** allowed: history, movie/TV, celebrity, sports,
   music, weird/mystery, old TV, internet history.
8. **Avoid excessive baked-in text** unless it carries the story.

---

## 5. Next steps

1. (Optional) Replace the 4 dead handles with correct ones if found.
2. Run the source-pooling workflow against the verified 6: scrape candidates →
   review/filter against these rules → download useful clips → accept into
   `history` → distribute.
3. Revisit rules after posting and measuring our own results (Phase 5 loop).
