from __future__ import annotations

import random

import pytest

from nicheflow_studio.core.distribution import (
    distribution_counts,
    plan_first_cycle,
)


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
