from __future__ import annotations

import datetime as dt

from nicheflow_studio.core import apify_usage


def test_usage_starts_at_zero() -> None:
    summary = apify_usage.monthly_apify_usage()
    assert summary["used"] == 0
    assert summary["remaining"] == apify_usage.APIFY_FREE_MONTHLY_RESULTS
    assert summary["estimated_cost_usd"] == 0
    assert summary["over_free_tier"] is False
    assert summary["warn"] is False


def test_record_accumulates_within_month() -> None:
    now = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
    apify_usage.record_apify_results(100, now=now)
    total = apify_usage.record_apify_results(50, now=now)
    assert total == 150
    summary = apify_usage.monthly_apify_usage(now=now)
    assert summary["used"] == 150
    assert summary["remaining"] == apify_usage.APIFY_FREE_MONTHLY_RESULTS - 150
    assert summary["estimated_cost_usd"] == 0.405


def test_warn_then_over_flags() -> None:
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    cap = apify_usage.APIFY_FREE_MONTHLY_RESULTS

    apify_usage.record_apify_results(int(cap * 0.8), now=now)
    warned = apify_usage.monthly_apify_usage(now=now)
    assert warned["warn"] is True
    assert warned["over_free_tier"] is False

    apify_usage.record_apify_results(cap, now=now)
    over = apify_usage.monthly_apify_usage(now=now)
    assert over["over_free_tier"] is True
    assert over["remaining"] == 0


def test_usage_is_scoped_per_month() -> None:
    june = dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc)
    july = dt.datetime(2026, 7, 10, tzinfo=dt.timezone.utc)
    apify_usage.record_apify_results(500, now=june)
    assert apify_usage.monthly_apify_usage(now=june)["used"] == 500
    assert apify_usage.monthly_apify_usage(now=july)["used"] == 0


def test_record_ignores_non_positive() -> None:
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    assert apify_usage.record_apify_results(0, now=now) == 0
    assert apify_usage.record_apify_results(-5, now=now) == 0
    assert apify_usage.monthly_apify_usage(now=now)["used"] == 0
