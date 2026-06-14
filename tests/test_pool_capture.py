from __future__ import annotations

import datetime as dt

import pytest

from nicheflow_studio.db.models import Account, PoolItem
from nicheflow_studio.db.pools import pool_size
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.scraper.youtube import ScrapedVideoCandidate
from nicheflow_studio.services import pool_capture
from nicheflow_studio.services.pool_capture import PoolCaptureError


def _candidate(url: str) -> ScrapedVideoCandidate:
    return ScrapedVideoCandidate(
        scrape_source_url=url,
        source_url=url,
        extractor="apify:instagram",
        video_id="ABC123",
        title="A useful history clip",
        channel_name="historysource",
        published_at=dt.datetime(2026, 6, 7, tzinfo=dt.timezone.utc),
        description="Useful metadata",
        view_count=1000,
        like_count=200,
        comment_count=12,
        duration_seconds=30,
        thumbnail_url="https://example.com/thumb.jpg",
    )


def test_normalize_instagram_media_url_canonicalizes_reels_route() -> None:
    assert (
        pool_capture.normalize_instagram_media_url(
            "https://www.instagram.com/reels/ABC123/" "?utm_source=test"
        )
        == "https://www.instagram.com/reel/ABC123/"
    )


def test_normalize_instagram_media_url_rejects_other_pages() -> None:
    with pytest.raises(PoolCaptureError):
        pool_capture.normalize_instagram_media_url(
            "https://www.instagram.com/historysource/"
        )  # noqa: E501


def test_capture_instagram_reel_to_pool_fetches_metadata_and_pools(
    monkeypatch,
) -> None:
    init_db()
    with get_session() as session:
        session.add(
            Account(name="Past Moments Daily", platform="instagram", niche="history")  # noqa: E501
        )  # noqa: E501
        session.commit()

    captured_urls: list[str] = []

    def fake_scrape(urls, *, results_limit):
        captured_urls.extend(urls)
        return [_candidate(urls[0])]

    monkeypatch.setattr(pool_capture, "scrape_instagram_urls_apify", fake_scrape)  # noqa: E501

    result = pool_capture.capture_instagram_reel_to_pool(
        "https://www.instagram.com/reels/ABC123/" "?utm_source=test"
    )

    assert result["status"] == "added"
    assert result["channel_name"] == "historysource"
    assert captured_urls == ["https://www.instagram.com/reel/ABC123/"]
    with get_session() as session:
        assert pool_size(session, "history") == 1


def test_capture_stores_optional_account_pin(monkeypatch) -> None:
    init_db()
    with get_session() as session:
        account = Account(name="Pinned History", platform="instagram", niche="history")
        session.add(account)
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        pool_capture,
        "scrape_instagram_urls_apify",
        lambda urls, *, results_limit: [_candidate(urls[0])],
    )

    result = pool_capture.capture_instagram_reel_to_pool(
        "https://www.instagram.com/reel/ABC123/",
        pinned_account_id=account_id,
    )

    assert result["status"] == "added"
    with get_session() as session:
        assert session.query(PoolItem).one().pinned_account_id == account_id


def test_duplicate_capture_skips_metadata_fetch(monkeypatch) -> None:
    init_db()
    with get_session() as session:
        session.add(
            Account(name="Past Moments Daily", platform="instagram", niche="history")  # noqa: E501
        )  # noqa: E501
        session.commit()

    monkeypatch.setattr(
        pool_capture,
        "scrape_instagram_urls_apify",
        lambda urls, *, results_limit: [_candidate(urls[0])],
    )
    first = pool_capture.capture_instagram_reel_to_pool(
        "https://www.instagram.com/reel/ABC123/"
    )  # noqa: E501

    def fail_if_called(*args, **kwargs):
        raise AssertionError("duplicate capture should not spend an Apify request")  # noqa: E501

    monkeypatch.setattr(pool_capture, "scrape_instagram_urls_apify", fail_if_called)  # noqa: E501
    second = pool_capture.capture_instagram_reel_to_pool(
        "https://www.instagram.com/reel/ABC123/"
    )  # noqa: E501

    assert first["status"] == "added"
    assert second["status"] == "duplicate"
    assert first["dashboard"]["pools"]["history"]["video_count"] == 1
    assert first["dashboard"]["apify_usage"]["used"] == 1
    assert second["dashboard"]["apify_usage"]["used"] == 1


def test_capture_dashboard_reports_each_pool() -> None:
    init_db()
    with get_session() as session:
        session.add_all(
            [
                Account(name="History Account", platform="instagram", niche="history"),
                Account(name="Movie Account", platform="instagram", niche="movie"),
            ]
        )
        session.commit()

    dashboard = pool_capture.capture_dashboard()

    assert dashboard["pools"]["history"]["video_count"] == 0
    assert dashboard["pools"]["movie"]["video_count"] == 0
    assert dashboard["pools"]["history"]["accounts"] == [
        {"id": dashboard["pools"]["history"]["accounts"][0]["id"], "name": "History Account"}
    ]
    assert dashboard["pools"]["movie"]["accounts"] == [
        {"id": dashboard["pools"]["movie"]["accounts"][0]["id"], "name": "Movie Account"}
    ]
    assert dashboard["apify_usage"]["estimated_cost_usd"] == 0


def test_batch_capture_uses_one_metadata_request(monkeypatch) -> None:
    init_db()
    with get_session() as session:
        session.add(
            Account(name="Past Moments Daily", platform="instagram", niche="history")  # noqa: E501
        )  # noqa: E501
        session.commit()

    calls: list[list[str]] = []

    def fake_scrape(urls, *, results_limit):
        calls.append(urls)
        return [
            ScrapedVideoCandidate(
                **{
                    **_candidate(url).__dict__,
                    "video_id": url.rstrip("/").rsplit("/", 1)[-1],
                }
            )
            for url in urls
        ]

    monkeypatch.setattr(pool_capture, "scrape_instagram_urls_apify", fake_scrape)  # noqa: E501
    batch = pool_capture.capture_instagram_reels_to_pool(
        [
            {"url": "https://www.instagram.com/reel/ONE/", "niche": "history"},
            {"url": "https://www.instagram.com/reel/TWO/", "niche": "history"},
        ]
    )

    assert len(calls) == 1
    assert len(calls[0]) == 2
    assert batch["summary"]["added"] == 2
    assert batch["summary"]["apify_results"] == 2
