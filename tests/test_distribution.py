from __future__ import annotations

import random

import pytest

from nicheflow_studio.core.distribution import (
    distribution_counts,
    plan_first_cycle,
    target_backlog,
)


def test_target_backlog_mvp_default_is_28() -> None:
    """MVP cadence: 4 posts/day over a 7-day window = 28 clips/account."""
    assert target_backlog() == 28


def test_target_backlog_scales_with_cadence_and_window() -> None:
    assert target_backlog(5, 7) == 35
    assert target_backlog(4, 30) == 120
    assert target_backlog(5, 30) == 150


def test_target_backlog_rejects_non_positive_inputs() -> None:
    with pytest.raises(ValueError):
        target_backlog(0, 7)
    with pytest.raises(ValueError):
        target_backlog(4, 0)


def test_each_clip_assigned_once_to_one_account() -> None:
    items = list(range(1, 301))  # 300 clips
    accounts = list(range(1, 11))  # 10 accounts
    plan = plan_first_cycle(items, accounts, rng=random.Random(1))

    assert len(plan) == 300
    # Every clip appears exactly once.
    assert sorted(a.pool_item_id for a in plan) == items
    # Only the given accounts are used.
    assert {a.account_id for a in plan} <= set(accounts)


def test_volume_is_balanced_across_accounts() -> None:
    items = list(range(1, 301))
    accounts = list(range(1, 11))
    plan = plan_first_cycle(items, accounts, rng=random.Random(7))

    counts = distribution_counts(plan)
    # 300 / 10 = 30 each, balanced to within one.
    assert set(counts.values()) <= {30}
    assert len(counts) == 10


def test_uneven_split_differs_by_at_most_one() -> None:
    items = list(range(1, 26))  # 25 clips
    accounts = list(range(1, 11))  # 10 accounts -> 2 or 3 each
    plan = plan_first_cycle(items, accounts, rng=random.Random(3))

    counts = distribution_counts(plan)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert sum(counts.values()) == 25


def test_max_per_account_caps_and_leaves_remainder_unassigned() -> None:
    items = list(range(1, 101))  # 100 clips
    accounts = [1, 2, 3]  # cap 10 each -> only 30 can be placed
    plan = plan_first_cycle(items, accounts, rng=random.Random(5), max_per_account=10)

    counts = distribution_counts(plan)
    assert len(plan) == 30
    assert all(v == 10 for v in counts.values())
    # The placed clips are still unique.
    assert len({a.pool_item_id for a in plan}) == 30


def test_empty_inputs_return_empty_plan() -> None:
    assert plan_first_cycle([], [1, 2], rng=random.Random(1)) == []
    assert plan_first_cycle([1, 2], [], rng=random.Random(1)) == []


def test_invalid_max_per_account_rejected() -> None:
    with pytest.raises(ValueError):
        plan_first_cycle([1], [1], rng=random.Random(1), max_per_account=0)


def test_deterministic_with_seeded_rng() -> None:
    items = list(range(1, 51))
    accounts = list(range(1, 6))
    a = plan_first_cycle(items, accounts, rng=random.Random(42))
    b = plan_first_cycle(items, accounts, rng=random.Random(42))
    assert a == b


def test_existing_counts_make_max_per_account_a_total_target() -> None:
    """With existing_counts, max_per_account is the TOTAL backlog target: an
    account already at target gets nothing; the under-filled one fills up."""
    items = list(range(1, 101))
    accounts = [1, 2]
    plan = plan_first_cycle(
        items,
        accounts,
        rng=random.Random(5),
        max_per_account=28,
        existing_counts={1: 28},  # account 1 already at target
    )
    counts = distribution_counts(plan)
    assert counts.get(1, 0) == 0  # already full -> no new assignments
    assert counts[2] == 28  # topped up to the target
    assert len(plan) == 28


def test_top_up_levels_underfilled_accounts_evenly() -> None:
    """Adding new accounts and re-running tops the new ones up to target and
    leaves the already-at-target accounts untouched."""
    items = list(range(1, 201))
    accounts = [1, 2, 3, 4, 5]
    plan = plan_first_cycle(
        items,
        accounts,
        rng=random.Random(9),
        max_per_account=28,
        existing_counts={1: 28, 2: 28},  # two old accounts already done
    )
    counts = distribution_counts(plan)
    assert counts.get(1, 0) == 0
    assert counts.get(2, 0) == 0
    assert counts[3] == 28
    assert counts[4] == 28
    assert counts[5] == 28
    assert len(plan) == 84  # 3 new accounts x 28


def test_top_up_spreads_evenly_when_pool_cannot_fill_all() -> None:
    """If the pool can't fill every under-target account, spread what's left
    evenly across the accounts with room (no overload, no starvation)."""
    items = list(range(1, 31))  # only 30 clips
    accounts = [1, 2, 3, 4, 5]
    plan = plan_first_cycle(
        items,
        accounts,
        rng=random.Random(3),
        max_per_account=28,
        existing_counts={1: 28, 2: 28},  # 3 accounts need filling, target 84
    )
    counts = distribution_counts(plan)
    assert counts.get(1, 0) == 0
    assert counts.get(2, 0) == 0
    # 30 clips across the 3 under-target accounts -> 10 each, even.
    assert counts[3] == 10
    assert counts[4] == 10
    assert counts[5] == 10
    assert len(plan) == 30


def test_existing_counts_none_matches_clean_first_cycle() -> None:
    """Omitting existing_counts must behave exactly like a clean first cycle:
    300 clips over 10 accounts -> 30 each."""
    items = list(range(1, 301))
    accounts = list(range(1, 11))
    plan = plan_first_cycle(items, accounts, rng=random.Random(7), max_per_account=28)
    counts = distribution_counts(plan)
    # max_per_account=28 caps each account; 10 x 28 = 280 placed, 20 left over.
    assert all(v == 28 for v in counts.values())
    assert len(plan) == 280
