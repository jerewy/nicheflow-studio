from __future__ import annotations

import datetime as dt
import random

import pytest

from nicheflow_studio.core.distribution import (
    distribution_counts,
    engagement_score,
    plan_first_cycle,
    ranked_clip_order,
    target_backlog,
)
from nicheflow_studio.core.engagement import (
    classify_topic_tier,
    source_engagement_rate,
    source_fit_score,
    suggested_action,
)


def test_target_backlog_mvp_default_is_28() -> None:
    """MVP cadence: 4 posts/day over a 7-day window = 28 clips/account."""
    assert target_backlog() == 28
    assert target_backlog(None, 7) == 28


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


def test_per_account_targets_fill_different_backlogs() -> None:
    plan = plan_first_cycle(
        list(range(1, 101)),
        [1, 2, 3],
        rng=random.Random(5),
        targets_by_account={1: 21, 2: 35, 3: 28},
    )

    assert distribution_counts(plan) == {1: 21, 2: 35, 3: 28}


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


# --------------------------------------------------------------------------- #
# engagement scoring + ranked ordering
# --------------------------------------------------------------------------- #


_NOW = dt.datetime(2026, 6, 8, tzinfo=dt.timezone.utc)


def test_source_engagement_rate_uses_views_as_the_denominator() -> None:
    assert source_engagement_rate(views=1_000, likes=40, comments=10) == pytest.approx(0.05)
    assert source_engagement_rate(views=0, likes=4, comments=1) == 5.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The last time they sang this song together", "S"),
        ("Remember this classic childhood cartoon theme song", "A"),
        ("Behind the scenes of the record-setting sports match", "B"),
        ("The actor was spotted at the appearance", "C"),
        ("A hydraulic press physics demo", "D"),
    ],
)
def test_classify_topic_tier_uses_seed_keywords(text: str, expected: str) -> None:
    assert classify_topic_tier(text) == expected


def test_suggested_action_is_advisory_from_tier_er_and_duration() -> None:
    # Short clips: engagement splits accept vs review.
    assert suggested_action("S", source_er=0.03, duration_seconds=30) == "accept"
    assert suggested_action("A", source_er=0.01, duration_seconds=30) == "review"
    # Tier C/D are always rejected, regardless of engagement or length.
    assert suggested_action("C", source_er=0.20, duration_seconds=20) == "reject"
    # Long clips (>35s) are only rejected when engagement is also weak; a long
    # clip that already engages well stays accept (the cap is ER-aware now).
    assert suggested_action("B", source_er=0.03, duration_seconds=36) == "accept"
    assert suggested_action("S", source_er=0.10, duration_seconds=120) == "accept"
    assert suggested_action("S", source_er=0.01, duration_seconds=60) == "reject"
    # Exactly at the short cap is not "long".
    assert suggested_action("A", source_er=0.01, duration_seconds=35) == "review"


def test_source_fit_score_retains_evergreen_safe_recency_weight() -> None:
    fresh = source_fit_score(tier="S", source_er=0.05, published_at=_NOW, now=_NOW)
    old = source_fit_score(
        tier="S",
        source_er=0.05,
        published_at=_NOW - dt.timedelta(days=365),
        now=_NOW,
    )
    assert fresh > old >= fresh * 0.5


def test_engagement_score_is_zero_without_likes() -> None:
    assert engagement_score(like_count=None) == 0.0
    assert engagement_score(like_count=0) == 0.0


def test_engagement_score_increases_with_likes() -> None:
    low = engagement_score(like_count=100, published_at=_NOW, now=_NOW)
    high = engagement_score(like_count=100_000, published_at=_NOW, now=_NOW)
    assert high > low > 0.0


def test_engagement_score_is_log_damped_not_linear() -> None:
    """100x the likes must NOT yield ~100x the score — mega-viral can't dominate."""
    low = engagement_score(like_count=1_000, published_at=_NOW, now=_NOW)
    high = engagement_score(like_count=100_000, published_at=_NOW, now=_NOW)
    assert high < low * 3  # log10 growth, far short of the 100x linear ratio


def test_engagement_score_decays_with_age() -> None:
    fresh = engagement_score(like_count=10_000, published_at=_NOW, now=_NOW)
    old = engagement_score(
        like_count=10_000,
        published_at=_NOW - dt.timedelta(days=365),
        now=_NOW,
    )
    assert fresh > old > 0.0


def test_engagement_score_recency_floors_at_half() -> None:
    """A very old clip keeps at least half its popularity weight (evergreen-safe)."""
    ancient = engagement_score(
        like_count=10_000,
        published_at=_NOW - dt.timedelta(days=36_500),
        now=_NOW,
    )
    no_recency = engagement_score(like_count=10_000)  # popularity only
    assert ancient >= no_recency * 0.5 - 1e-9


def test_ranked_clip_order_puts_strongest_tier_first() -> None:
    # Scores 1..20; top tier (highest scores) must all precede the bottom tier.
    scored = [(i, float(i)) for i in range(1, 21)]
    order = ranked_clip_order(scored, rng=random.Random(1), tier_fraction=0.25)
    assert sorted(order) == [i for i, _ in scored]  # permutation, nothing lost
    top_tier = set(order[:5])
    bottom_tier = set(order[-5:])
    assert top_tier == {16, 17, 18, 19, 20}  # 5 highest scores, in some order
    assert bottom_tier == {1, 2, 3, 4, 5}  # 5 lowest scores


def test_ranked_clip_order_jitters_within_a_tier() -> None:
    # Equal scores -> different seeds should yield different orders (jitter on).
    scored = [(i, 1.0) for i in range(1, 13)]
    a = ranked_clip_order(scored, rng=random.Random(1))
    b = ranked_clip_order(scored, rng=random.Random(2))
    assert sorted(a) == sorted(b) == [i for i, _ in scored]
    assert a != b


def test_ranked_clip_order_empty() -> None:
    assert ranked_clip_order([], rng=random.Random(1)) == []


def test_plan_first_cycle_preserves_order_when_not_shuffling() -> None:
    """With shuffle_items=False the best clips (front of the list) are placed
    first: one account each, in order, before any account gets a second clip."""
    items = [10, 20, 30, 40, 50, 60]  # already ranked best-first
    accounts = [1, 2, 3]
    plan = plan_first_cycle(
        items, accounts, rng=random.Random(0), max_per_account=1, shuffle_items=False
    )
    # Only the top 3 clips are placed (cap 1 each), and they are the first three.
    assert {a.pool_item_id for a in plan} == {10, 20, 30}
