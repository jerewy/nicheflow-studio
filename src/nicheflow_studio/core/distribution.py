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


def plan_first_cycle(
    pool_item_ids: list[int],
    account_ids: list[int],
    *,
    rng: _random.Random | None = None,
    max_per_account: int | None = None,
    existing_counts: dict[int, int] | None = None,
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
