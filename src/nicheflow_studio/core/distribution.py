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
        Optional cap on how many clips one account receives this cycle. Once
        every account is full, remaining clips are left unassigned (returned
        assignments simply stop) — they wait for the next cycle / more accounts.

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

    assignments: list[PlannedAssignment] = []
    per_account: Counter[int] = Counter()
    account_count = len(accounts)

    for index, pool_item_id in enumerate(items):
        # Round-robin over the shuffled account order for even volume.
        account_id = accounts[index % account_count]
        if max_per_account is not None:
            if per_account[account_id] >= max_per_account:
                # This slot is full; try to place it on any account with room.
                account_id = _first_account_with_room(accounts, per_account, max_per_account)
                if account_id is None:
                    break  # every account is full — stop, leave the rest unassigned
        assignments.append(
            PlannedAssignment(pool_item_id=pool_item_id, account_id=account_id)
        )
        per_account[account_id] += 1

    return assignments


def _first_account_with_room(
    accounts: list[int],
    per_account: "Counter[int]",
    max_per_account: int,
) -> int | None:
    for account_id in accounts:
        if per_account[account_id] < max_per_account:
            return account_id
    return None


def distribution_counts(assignments: list[PlannedAssignment]) -> dict[int, int]:
    """How many clips each account received — for a pre-commit preview."""
    counts: Counter[int] = Counter()
    for assignment in assignments:
        counts[assignment.account_id] += 1
    return dict(counts)
