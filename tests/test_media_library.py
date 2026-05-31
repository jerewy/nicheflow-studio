from __future__ import annotations

import pytest

from nicheflow_studio.core.niche import NICHE_HISTORY, NICHE_MOVIE, classify_niche
from nicheflow_studio.db.media_library import (
    extract_instagram_shortcode,
    find_media_asset,
    find_or_register_media_asset,
    mark_media_asset_downloaded,
    normalize_source_url,
)
from nicheflow_studio.db.models import Account
from nicheflow_studio.db.session import get_session, init_db

REEL = "https://www.instagram.com/reel/AbC123xyz/"


# --- niche classification (pure) -------------------------------------------


def test_classify_niche_history_and_movie() -> None:
    assert classify_niche("History moments, old clips, forgotten stories") == NICHE_HISTORY
    assert classify_niche("Movie scenes, cinematic moments") == NICHE_MOVIE
    assert classify_niche("Relatable daily cope memes") is None
    assert classify_niche(None) is None


# --- URL / shortcode normalization (pure) ----------------------------------


def test_normalize_source_url_strips_query_and_trailing_slash() -> None:
    assert normalize_source_url("https://www.instagram.com/reel/AbC123xyz/?igshid=9") == (
        "https://www.instagram.com/reel/AbC123xyz"
    )
    # Host lowercased, shortcode case preserved.
    assert normalize_source_url("https://WWW.Instagram.com/p/AbC/") == (
        "https://www.instagram.com/p/AbC"
    )


def test_extract_instagram_shortcode_handles_reel_p_tv() -> None:
    assert extract_instagram_shortcode("https://www.instagram.com/reel/AbC123xyz/") == "AbC123xyz"
    assert extract_instagram_shortcode("https://www.instagram.com/p/ZZ9/?x=1") == "ZZ9"
    assert extract_instagram_shortcode("https://example.com/video/123") is None


# --- dedup gate (DB) -------------------------------------------------------


def test_find_or_register_creates_then_dedupes(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset, created = find_or_register_media_asset(session, source_url=REEL)
        session.commit()
        assert created is True
        assert asset.source_shortcode == "AbC123xyz"
        assert asset.download_status == "pending"

        # Same reel again -> same row, not a second download.
        again, created_again = find_or_register_media_asset(session, source_url=REEL)
        assert created_again is False
        assert again.id == asset.id


def test_dedup_matches_same_shortcode_from_different_url(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset, _ = find_or_register_media_asset(session, source_url=REEL)
        session.commit()
        # Same shortcode, /p/ instead of /reel/ and a query string.
        match = find_media_asset(
            session, source_url="https://www.instagram.com/p/AbC123xyz/?igshid=7"
        )
        assert match is not None
        assert match.id == asset.id


def test_mark_downloaded_records_path_and_status(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset, _ = find_or_register_media_asset(session, source_url=REEL)
        mark_media_asset_downloaded(
            asset, original_download_path="data/media/AbC123xyz.mp4", file_size_bytes=1234
        )
        session.commit()
        assert asset.download_status == "downloaded"
        assert asset.original_download_path == "data/media/AbC123xyz.mp4"
        assert asset.downloaded_at is not None


def test_find_or_register_requires_source_url(tmp_path) -> None:
    init_db()
    with get_session() as session:
        with pytest.raises(ValueError):
            find_or_register_media_asset(session, source_url="")


# --- niche column + backfill (DB) ------------------------------------------


def test_account_niche_backfills_from_label_on_init(tmp_path) -> None:
    init_db()
    with get_session() as session:
        session.add_all(
            [
                Account(name="Past Moments Daily", niche_label="History moments, old clips"),
                Account(name="Cinema Files Daily", niche_label="Movie scenes, cinematic moments"),
                Account(name="Memeists Daily", niche_label="Relatable daily cope memes"),
                Account(name="Manual", niche_label="History but pinned", niche="movie"),
            ]
        )
        session.commit()

    # Re-run the backfill (idempotent; only fills NULLs).
    from nicheflow_studio.db.session import _backfill_account_niche

    _backfill_account_niche()

    with get_session() as session:
        by_name = {a.name: a.niche for a in session.query(Account).all()}
    assert by_name["Past Moments Daily"] == "history"
    assert by_name["Cinema Files Daily"] == "movie"
    assert by_name["Memeists Daily"] is None  # meme doesn't classify
    assert by_name["Manual"] == "movie"  # explicit niche never overwritten
