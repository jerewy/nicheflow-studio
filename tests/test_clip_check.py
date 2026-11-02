from __future__ import annotations

import datetime as dt

import pytest

from nicheflow_studio.core import clip_check

NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc)
TEN_DAYS_AGO = NOW - dt.timedelta(days=10)


def _check(**overrides) -> clip_check.ClipCheck:
    fields = {
        "views": 300_000,
        "likes": 12_000,
        "comments": 340,
        "published_at": TEN_DAYS_AGO,
        "channel_name": "historytrails",
        "text": "Princess Diana answers the palace rumours in 1996",
        "baseline_views": 100_000,
        "now": NOW,
    }
    fields.update(overrides)
    return clip_check.check_clip(**fields)


@pytest.mark.parametrize(
    ("views", "verdict"),
    [(250_000, "strong"), (200_000, "strong"), (150_000, "ok"), (100_000, "ok"), (99_000, "weak")],
)
def test_verdict_follows_views_against_creator_normal(views: int, verdict: str) -> None:
    assert _check(views=views).verdict == verdict


def test_unknown_when_creator_normal_is_missing() -> None:
    check = _check(baseline_views=None)

    assert check.verdict == "unknown"
    assert check.ratio is None
    assert "creator's normal views unknown" in check.flags


def test_unknown_when_view_count_is_missing() -> None:
    assert _check(views=None).verdict == "unknown"


def test_three_million_views_caps_strong_at_ok() -> None:
    check = _check(views=4_000_000, baseline_views=500_000)

    assert check.ratio == pytest.approx(8.0)
    assert check.verdict == "ok"
    assert "3M+ views, probably seen already" in check.flags


def test_young_clip_is_flagged_still_rising_without_changing_verdict() -> None:
    check = _check(published_at=NOW - dt.timedelta(days=1))

    assert check.verdict == "strong"
    assert "still rising" in check.flags


@pytest.mark.parametrize(
    "caption",
    [
        "Use code HISTORY for 20% off",
        "Paid partnership with a snack brand",
        "Watch till the end #ad",
    ],
)
def test_paid_promotion_is_weak_even_with_big_views(caption: str) -> None:
    check = _check(text=caption, views=900_000)

    assert check.verdict == "weak"
    assert "looks like a paid promotion" in check.flags


def test_concert_tickets_caption_is_not_mistaken_for_an_ad() -> None:
    assert _check(text="Tickets sold out in minutes for this 1985 show").verdict == "strong"


def test_hydraulic_press_is_excluded_but_tennis_for_two_is_not() -> None:
    assert _check(text="A wolf tooth under a hydraulic press").verdict == "weak"
    assert _check(text="Tennis for Two ran on an oscilloscope in 1958").verdict == "strong"


def test_summary_reads_like_a_verdict_line() -> None:
    summary = _check(views=412_000, likes=18_000, comments=320, baseline_views=121_000).summary

    assert summary == (
        "Strong: 3.4x normal for @historytrails (412k vs 121k) | 18k likes"
        " | 320 comments | 10 days old"
    )


def test_summary_skips_hidden_like_counts() -> None:
    assert "likes" not in _check(likes=-1).summary


def test_median_views_needs_enough_posts() -> None:
    assert clip_check.median_views([10, 20, 30, 40]) is None
    assert clip_check.median_views([10, 20, 30, 40, 50, None, 0]) == 30


@pytest.mark.parametrize(
    ("value", "text"),
    [(950, "950"), (1_250, "1.2k"), (1_000, "1k"), (412_345, "412k"), (3_400_000, "3.4M"), (2_000_000, "2M")],
)
def test_compact_number(value: int, text: str) -> None:
    assert clip_check.compact_number(value) == text
