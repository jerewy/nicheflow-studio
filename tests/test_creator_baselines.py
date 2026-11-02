from __future__ import annotations

import datetime as dt

import pytest

from nicheflow_studio.db.models import Account, CreatorBaseline, ScrapeCandidate
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.scraper.instagram_apify import ApifyScrapeError, RecentReel
from nicheflow_studio.services import creator_baselines

NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc)


def _settled(views: list[int]) -> list[RecentReel]:
    return [RecentReel(count, NOW - dt.timedelta(days=10)) for count in views]


def _fail_if_called(names):
    raise AssertionError(f"Apify should not be called, got {names!r}")


def _save_baseline(channel: str, median: int, source: str, age_days: int) -> None:
    with get_session() as session:
        session.add(
            CreatorBaseline(
                channel_name=channel,
                median_views=median,
                sample_size=12,
                source=source,
                measured_at=NOW - dt.timedelta(days=age_days),
            )
        )
        session.commit()


def _add_scraped_rows(channel: str, views: list[int]) -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        session.add(account)
        session.flush()
        for index, count in enumerate(views):
            url = f"https://www.instagram.com/p/{channel}{index}/"
            session.add(
                ScrapeCandidate(
                    scrape_source_url=url,
                    source_url=url,
                    channel_name=channel,
                    view_count=count,
                    account_id=account.id,
                )
            )
        session.commit()


def _resolve(channels, **kwargs) -> dict[str, int]:
    with get_session() as session:
        result = creator_baselines.resolve_baselines(session, channels, now=NOW, **kwargs)
        session.commit()
    return result


def test_fresh_saved_baseline_is_used_without_calling_apify(monkeypatch) -> None:
    init_db()
    _save_baseline("historytrails", 120_000, "apify", age_days=3)
    monkeypatch.setattr(creator_baselines, "fetch_instagram_profile_views_apify", _fail_if_called)

    assert _resolve(["@HistoryTrails"]) == {"historytrails": 120_000}


def test_missing_creators_are_fetched_in_one_call_and_saved(monkeypatch) -> None:
    init_db()
    calls: list[list[str]] = []

    def fake_fetch(names):
        calls.append(list(names))
        return {
            "alpha": _settled([100, 200, 300, 400, 500]),
            "beta": _settled([10, 20, 30, 40, 50, 60]),
        }

    monkeypatch.setattr(creator_baselines, "fetch_instagram_profile_views_apify", fake_fetch)

    assert _resolve(["beta", "Alpha", "beta"]) == {"alpha": 300, "beta": 35}
    assert calls == [["alpha", "beta"]]
    with get_session() as session:
        saved = {row.channel_name: row for row in session.query(CreatorBaseline)}
    assert saved["alpha"].source == "apify"
    assert saved["alpha"].sample_size == 5


def test_stale_baseline_is_refreshed(monkeypatch) -> None:
    init_db()
    _save_baseline("historytrails", 120_000, "apify", age_days=20)
    monkeypatch.setattr(
        creator_baselines,
        "fetch_instagram_profile_views_apify",
        lambda names: {"historytrails": _settled([50_000] * 6)},
    )

    assert _resolve(["historytrails"]) == {"historytrails": 50_000}


def test_falls_back_to_our_bulk_scrapes_when_apify_fails(monkeypatch) -> None:
    init_db()
    _add_scraped_rows("crazyfactscorner", list(range(1_000, 31_000, 1_000)))

    def failing_fetch(names):
        raise ApifyScrapeError("Actor run failed")

    monkeypatch.setattr(creator_baselines, "fetch_instagram_profile_views_apify", failing_fetch)

    assert _resolve(["crazyfactscorner"]) == {"crazyfactscorner": 15_500}
    with get_session() as session:
        assert session.query(CreatorBaseline).one().source == "pool"


def test_creator_known_only_from_hand_captures_gets_no_baseline(monkeypatch) -> None:
    init_db()
    # Three hand-picked winners are not this creator's "normal".
    _add_scraped_rows("therealhistoryy", [4_000_000, 5_000_000, 6_000_000])
    monkeypatch.setattr(creator_baselines, "fetch_instagram_profile_views_apify", lambda names: {})

    assert _resolve(["therealhistoryy"]) == {}


def test_review_mode_never_calls_apify_and_trusts_a_pool_baseline(monkeypatch) -> None:
    init_db()
    _save_baseline("historytrails", 64_000, "pool", age_days=2)
    monkeypatch.setattr(creator_baselines, "fetch_instagram_profile_views_apify", _fail_if_called)

    assert _resolve(["historytrails", "newcreator"], fetch_missing=False) == {"historytrails": 64_000}


def test_capture_mode_prefers_current_numbers_over_a_pool_baseline(monkeypatch) -> None:
    init_db()
    _save_baseline("historytrails", 64_000, "pool", age_days=2)
    monkeypatch.setattr(
        creator_baselines,
        "fetch_instagram_profile_views_apify",
        lambda names: {"historytrails": _settled([90_000] * 8)},
    )

    assert _resolve(["historytrails"]) == {"historytrails": 90_000}


@pytest.mark.parametrize("channels", [[], [None, "", "  "]])
def test_no_channels_means_no_work(monkeypatch, channels) -> None:
    init_db()
    monkeypatch.setattr(creator_baselines, "fetch_instagram_profile_views_apify", _fail_if_called)

    assert _resolve(channels) == {}


def test_reels_still_climbing_are_left_out_of_the_normal(monkeypatch) -> None:
    init_db()
    fresh = [RecentReel(3_000, NOW - dt.timedelta(hours=hours)) for hours in (2, 10, 20, 30, 40, 50)]
    settled = _settled([60_000, 70_000, 80_000, 90_000, 100_000])
    monkeypatch.setattr(
        creator_baselines,
        "fetch_instagram_profile_views_apify",
        lambda names: {"historytrails": fresh + settled},
    )

    assert _resolve(["historytrails"]) == {"historytrails": 80_000}


def test_creator_with_only_fresh_reels_falls_back_to_our_scrapes(monkeypatch) -> None:
    init_db()
    _add_scraped_rows("historytrails", [64_000] * 30)
    fresh = [RecentReel(5_000, NOW - dt.timedelta(hours=hours)) for hours in range(2, 48, 4)]
    monkeypatch.setattr(
        creator_baselines, "fetch_instagram_profile_views_apify", lambda names: {"historytrails": fresh}
    )

    assert _resolve(["historytrails"]) == {"historytrails": 64_000}
    with get_session() as session:
        assert session.query(CreatorBaseline).one().source == "pool_checked"

    # The next capture must not pay Apify again for the same unusable answer.
    monkeypatch.setattr(creator_baselines, "fetch_instagram_profile_views_apify", _fail_if_called)
    assert _resolve(["historytrails"]) == {"historytrails": 64_000}
