"""First-cycle pool→account distribution (pure, no DB/Qt).

Turns "N accepted clips in a niche pool" into "which account posts which clip",
for the first launch cycle where each clip goes to exactly ONE account
(docs/SOURCING_POOLING_PLAN.md §12.1). Properties:

- **Niche isolation is the caller's job**: only pass accounts + pool items from
  the SAME niche. This module never mixes niches because it never sees them.
- **Each clip assigned once** (no reuse in the first cycle).
- **Balanced volume**: round-robin over a shuffled account order, so per-account
  counts differ by at most one — avoids the lumpiness of pure random assignment
  while still randomizing which account gets which clip.

Kept pure so it is trivially testable and reusable from a future web backend.
"""
from __future__ import annotations

import datetime as dt
import math
import random as _random
from collections import Counter
from dataclasses import dataclass


# MVP posting cadence used to size each account's backlog. Distribution fills
# every account up to `daily posts x planning window`, NOT a magic constant like
# 100 (docs/SOURCING_POOLING_PLAN.md §4). Kept as module defaults for the first
# cut; can later move to a per-account field or a global setting.
DEFAULT_DAILY_POSTS_PER_ACCOUNT = 4
DEFAULT_PLANNING_WINDOW_DAYS = 7


def target_backlog(
    daily_posts_per_account: int = DEFAULT_DAILY_POSTS_PER_ACCOUNT,
    planning_window_days: int = DEFAULT_PLANNING_WINDOW_DAYS,
) -> int:
    """Per-account backlog target = daily posts × planning window (in days).

    Replaces hardcoded distribution sizes: how many clips an account should hold
    is driven by how fast it posts and how far ahead we plan. MVP default is
    4 × 7 = 28. See docs/SOURCING_POOLING_PLAN.md §4.
    """
    if daily_posts_per_account < 1:
        raise ValueError("daily_posts_per_account must be at least 1.")
    if planning_window_days < 1:
        raise ValueError("planning_window_days must be at least 1.")
    return daily_posts_per_account * planning_window_days


@dataclass(frozen=True)
class PlannedAssignment:
    """One planned clip→account pairing (ids only; persistence is separate)."""

    pool_item_id: int
    account_id: int


# Default recency half-life for the engagement score: a clip's like signal is
# halved every ~6 months, so a fresh clip edges out an equally-liked old one
# (older reposts are more saturated on-platform) without erasing strong evergreens.
DEFAULT_RECENCY_HALF_LIFE_DAYS = 180.0
# Default tier size for ranked_clip_order, as a fraction of the pool. 0.25 = four
# tiers; clips are distributed best-tier-first, shuffled WITHIN a tier so the
# network doesn't funnel the single top clip onto every account (which would
# maximize cross-account duplication — the main originality risk).
DEFAULT_TIER_FRACTION = 0.25


def _age_days(published_at: dt.datetime | None, now: dt.datetime) -> float:
    """Age of a post in days, tz-robust. SQLite hands back naive datetimes while
    ``now`` is usually aware, so both are flattened to naive before subtracting.
    Missing or future dates clamp to 0 (treated as brand new)."""
    if published_at is None:
        return 0.0
    published_naive = published_at.replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None)
    return max(0.0, (now_naive - published_naive).total_seconds() / 86400.0)


def engagement_score(
    *,
    like_count: int | None,
    published_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
    recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Intrinsic "worth distributing" score for a pool clip.

    Two ideas, both deliberate (see the distribution-strategy discussion):

    1. **log-damped likes** — ``log10(1 + likes)`` so a mega-viral clip doesn't
       dwarf everything and the raw follower-size/age bias of likes is blunted.
       Proven engagement still ranks higher; it just isn't winner-take-all.
    2. **gentle recency decay** — fresher clips edge out equally-liked older ones
       because old viral clips are the most reposted/saturated on-platform. The
       decay floors at 0.5 so a strong evergreen never drops below half its
       popularity weight.

    Returns ``0.0`` when there is no like signal, so clips with no metadata sink
    to the bottom instead of being randomly favored.
    """
    likes = max(0, int(like_count or 0))
    popularity = math.log10(1.0 + likes)
    if popularity <= 0.0:
        return 0.0
    if published_at is None:
        return popularity
    now = now or dt.datetime.now(dt.timezone.utc)
    if recency_half_life_days <= 0:
        return popularity
    recency = 0.5 ** (_age_days(published_at, now) / recency_half_life_days)
    return popularity * (0.5 + 0.5 * recency)


def ranked_clip_order(
    scored: list[tuple[int, float]],
    *,
    rng: _random.Random | None = None,
    tier_fraction: float = DEFAULT_TIER_FRACTION,
) -> list[int]:
    """Order pool-item ids best-first, randomized WITHIN score tiers.

    Pure ranking step used before :func:`plan_first_cycle`: sort by score
    descending, split into equal tiers, then shuffle each tier. The strongest
    clips still go out first (quality), but the exact #1/#2/#3 ordering is
    jittered so re-running doesn't deterministically push the same single clip to
    the front for every account — combined with one-clip-one-account assignment
    this spreads unique strong clips instead of cloning the top viral one.

    ``scored`` is ``(pool_item_id, score)`` pairs. Returns just the ordered ids.
    """
    rng = rng or _random.Random()
    if not scored:
        return []
    ordered = sorted(scored, key=lambda pair: pair[1], reverse=True)
    count = len(ordered)
    tier_size = max(1, round(count * tier_fraction)) if tier_fraction > 0 else count
    result: list[int] = []
    for start in range(0, count, tier_size):
        tier_ids = [pool_item_id for pool_item_id, _ in ordered[start : start + tier_size]]
        rng.shuffle(tier_ids)
        result.extend(tier_ids)
    return result


def plan_first_cycle(
    pool_item_ids: list[int],
    account_ids: list[int],
    *,
    rng: _random.Random | None = None,
    max_per_account: int | None = None,
    existing_counts: dict[int, int] | None = None,
    shuffle_items: bool = True,
) -> list[PlannedAssignment]:
    """Assign each pool item to exactly one account, balanced and shuffled.

    Parameters
    ----------
    pool_item_ids:
        Accepted, unassigned pool items in ONE niche.
    account_ids:
        Destination accounts in that SAME niche.
    rng:
        Inject a seeded ``random.Random`` for deterministic tests.
    max_per_account:
        Optional cap on how many clips one account may hold. When
        ``existing_counts`` is given this is the TOTAL backlog target per
        account (existing + new), not a per-run cap — so re-running to "top up"
        is idempotent: an account already at target receives nothing more. Once
        every account is full, remaining clips are left unassigned (returned
        assignments simply stop) — they wait for the next cycle / more accounts.
    existing_counts:
        How many clips each account already holds from earlier cycles
        (``account_id -> count``). Used to make ``max_per_account`` a running
        total so "Top Up" levels under-filled accounts without touching the
        ones already at target. Omit (or pass empty) for a clean first cycle,
        where every account starts at zero.

    Returns
    -------
    A list of :class:`PlannedAssignment`. Empty if there are no accounts or no
    pool items.
    """
    if not pool_item_ids or not account_ids:
        return []
    if max_per_account is not None and max_per_account < 1:
        raise ValueError("max_per_account must be at least 1 when set.")

    rng = rng or _random.Random()
    items = list(pool_item_ids)
    accounts = list(account_ids)
    # shuffle_items=False preserves a caller-supplied order (e.g. an engagement
    # ranking from ranked_clip_order) so the strongest clips are placed first.
    if shuffle_items:
        rng.shuffle(items)
    rng.shuffle(accounts)

    # Seed each account's running count with the backlog it already holds so
    # max_per_account behaves as a TOTAL target. With no existing counts every
    # account starts at zero and this matches a clean first cycle.
    base = existing_counts or {}
    per_account: Counter[int] = Counter({acct: int(base.get(acct, 0)) for acct in accounts})

    def _has_room(account_id: int) -> bool:
        return max_per_account is None or per_account[account_id] < max_per_account

    assignments: list[PlannedAssignment] = []
    for pool_item_id in items:
        # Fill the emptiest account that still has room first (ties broken by the
        # shuffled account order). This keeps per-account volume even to within
        # one across accounts that have capacity — even when some started this
        # run already full from a previous cycle.
        eligible = [account_id for account_id in accounts if _has_room(account_id)]
        if not eligible:
            break  # every account is at target — leave the rest unassigned
        account_id = min(eligible, key=lambda acct: per_account[acct])
        assignments.append(
            PlannedAssignment(pool_item_id=pool_item_id, account_id=account_id)
        )
        per_account[account_id] += 1

    return assignments


def distribution_counts(assignments: list[PlannedAssignment]) -> dict[int, int]:
    """How many clips each account received — for a pre-commit preview."""
    counts: Counter[int] = Counter()
    for assignment in assignments:
        counts[assignment.account_id] += 1
    return dict(counts)
