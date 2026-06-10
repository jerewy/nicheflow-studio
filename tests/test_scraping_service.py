from __future__ import annotations

import pytest

from nicheflow_studio.db.models import Account, DownloadItem, PoolItem, ScrapeCandidate, Source
from nicheflow_studio.db.session import get_session
from nicheflow_studio.scraper.youtube import ScrapedVideoCandidate
from nicheflow_studio.services import scraping
from nicheflow_studio.services.scraping import ScrapingError


def _make_source(niche: str | None = "history") -> int:
    with get_session() as session:
        account = Account(name="Past Moments", platform="instagram", niche=niche)
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@thehistologian",
            source_url="https://www.instagram.com/thehistologian/",
        )
        session.add(source)
        session.commit()
        return source.id


def _fake_candidate(shortcode: str) -> ScrapedVideoCandidate:
    return ScrapedVideoCandidate(
        scrape_source_url="https://www.instagram.com/thehistologian/",
        source_url=f"https://www.instagram.com/reel/{shortcode}/",
        extractor="apify:instagram",
        video_id=shortcode,
        title=f"Clip {shortcode}",
        channel_name="thehistologian",
        published_at=None,
    )


def test_scrape_source_pools_and_dedups(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _make_source()
    # Third row repeats the first shortcode -> pool-first dedup should skip it.
    fakes = [_fake_candidate("AAA"), _fake_candidate("BBB"), _fake_candidate("AAA")]
    monkeypatch.setattr(scraping, "scrape_instagram_source_apify", lambda **kwargs: fakes)

    result = scraping.scrape_source_to_pool(source_id, max_items=10)

    assert result["scraped"] == 3
    assert result["added"] == 2
    assert result["duplicates"] == 1
    assert result["apify_usage"]["used"] == 3  # Apify bills per returned result
    with get_session() as session:
        assert session.query(PoolItem).count() == 2


def test_scrape_requires_account_niche(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _make_source(niche=None)
    monkeypatch.setattr(scraping, "scrape_instagram_source_apify", lambda **kwargs: [])
    with pytest.raises(ScrapingError):
        scraping.scrape_source_to_pool(source_id)


def test_scrape_unknown_source_raises() -> None:
    with pytest.raises(ScrapingError):
        scraping.scrape_source_to_pool(999999)


def test_scrape_records_run_status(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _make_source()
    monkeypatch.setattr(
        scraping, "scrape_instagram_source_apify", lambda **kwargs: [_fake_candidate("XYZ")]
    )

    scraping.scrape_source_to_pool(source_id)

    with get_session() as session:
        source = session.get(Source, source_id)
        assert source.last_run_status == "completed"
        assert source.last_scraped_at is not None


def test_scrape_failure_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = _make_source()

    def _boom(**kwargs):
        raise RuntimeError("apify down")

    monkeypatch.setattr(scraping, "scrape_instagram_source_apify", _boom)

    with pytest.raises(ScrapingError):
        scraping.scrape_source_to_pool(source_id)
    with get_session() as session:
        source = session.get(Source, source_id)
        assert source.last_run_status == "error"
        assert "apify down" in (source.last_error_summary or "")


def _make_candidate(account_id: int, *, source_url: str, video_id: str) -> int:
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s",
            source_url=source_url,
            video_id=video_id,
            title="Clip",
            state="candidate",
            account_id=account_id,
        )
        session.add(candidate)
        session.commit()
        return candidate.id


def _history_account() -> int:
    with get_session() as session:
        account = Account(name="Past Moments", platform="instagram", niche="history")
        session.add(account)
        session.commit()
        return account.id


def test_add_candidate_reuses_existing_file(tmp_path) -> None:
    from nicheflow_studio.db.media_library import (
        find_or_register_media_asset,
        mark_media_asset_downloaded,
    )

    account_id = _history_account()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    url = "https://www.instagram.com/reel/ABC/"
    with get_session() as session:
        asset, _ = find_or_register_media_asset(
            session, source_url=url, shortcode="ABC", platform="instagram"
        )
        mark_media_asset_downloaded(asset, original_download_path=str(video))
        session.commit()
    candidate_id = _make_candidate(account_id, source_url=url, video_id="ABC")

    result = scraping.add_candidate_to_processing(candidate_id)

    assert result["reused"] is True
    assert result["downloaded"] is False
    with get_session() as session:
        item = session.get(DownloadItem, result["item_id"])
        assert item.file_path == str(video)
        assert item.account_id == account_id
        assert item.review_state == "new"
        candidate = session.get(ScrapeCandidate, candidate_id)
        assert candidate.queued_download_item_id == result["item_id"]
        assert candidate.state == "downloaded"


def test_add_candidate_downloads_when_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nicheflow_studio.db.media_library import find_media_asset
    from nicheflow_studio.downloader.instagram import InstagramDownloadResult

    account_id = _history_account()
    url = "https://www.instagram.com/reel/DEF/"
    candidate_id = _make_candidate(account_id, source_url=url, video_id="DEF")
    downloaded_file = tmp_path / "DEF.mp4"
    downloaded_file.write_bytes(b"vid")

    seen = {}

    def fake_download(*, url, output_dir):
        seen["url"] = url
        return InstagramDownloadResult(
            extractor="instagram", video_id="DEF", title="t", file_path=downloaded_file
        )

    monkeypatch.setattr(scraping, "download_instagram_url", fake_download)

    result = scraping.add_candidate_to_processing(candidate_id)

    assert result["downloaded"] is True
    assert result["reused"] is False
    assert seen["url"] == url
    with get_session() as session:
        item = session.get(DownloadItem, result["item_id"])
        assert item.file_path == str(downloaded_file)
        asset = find_media_asset(session, source_url=url, shortcode="DEF")
        assert asset is not None
        assert asset.original_download_path == str(downloaded_file)


def test_add_candidate_idempotent_when_already_queued(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from nicheflow_studio.downloader.instagram import InstagramDownloadResult

    account_id = _history_account()
    url = "https://www.instagram.com/reel/GHI/"
    candidate_id = _make_candidate(account_id, source_url=url, video_id="GHI")
    downloaded_file = tmp_path / "GHI.mp4"
    downloaded_file.write_bytes(b"v")
    monkeypatch.setattr(
        scraping,
        "download_instagram_url",
        lambda *, url, output_dir: InstagramDownloadResult(
            extractor="instagram", video_id="GHI", title=None, file_path=downloaded_file
        ),
    )

    first = scraping.add_candidate_to_processing(candidate_id)
    second = scraping.add_candidate_to_processing(candidate_id)

    assert second["item_id"] == first["item_id"]
    assert second["reused"] is True
    with get_session() as session:
        assert len(session.scalars(select(DownloadItem)).all()) == 1


def test_add_candidate_unknown_raises() -> None:
    with pytest.raises(ScrapingError):
        scraping.add_candidate_to_processing(999999)
