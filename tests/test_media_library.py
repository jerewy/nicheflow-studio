from __future__ import annotations

import pytest

from nicheflow_studio.core.niche import NICHE_HISTORY, NICHE_MOVIE, classify_niche
from nicheflow_studio.db.media_library import (
    MIN_PLAUSIBLE_VIDEO_BYTES,
    BlankDownloadError,
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
            asset,
            original_download_path="data/media/AbC123xyz.mp4",
            file_size_bytes=1_800_000,
            content_hash="a1b2c3d4e5f60718,0053c8add2cd4a09",
        )
        session.commit()
        assert asset.download_status == "downloaded"
        assert asset.original_download_path == "data/media/AbC123xyz.mp4"
        assert asset.downloaded_at is not None


# --- blank-download guard --------------------------------------------------
#
# Instagram answers a request it does not like with a 1-second black clip
# instead of an error, and yt-dlp reports that as a normal download. In June
# 2026 that put 24 placeholders into the pool as "downloaded" footage; they
# surfaced months later as untitled reels with black contact sheets.


def test_mark_downloaded_rejects_a_placeholder_sized_file(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset, _ = find_or_register_media_asset(session, source_url=REEL)
        with pytest.raises(BlankDownloadError):
            mark_media_asset_downloaded(
                asset,
                original_download_path="data/media/AbC123xyz.mp4",
                # The exact size Instagram's black placeholder lands at.
                file_size_bytes=2479,
            )
        # Left pending, not "downloaded" and not pointed at the stub, so the
        # next run retries instead of skipping it as already on disk.
        assert asset.download_status == "pending"
        assert asset.original_download_path is None
        assert asset.downloaded_at is None


def test_mark_downloaded_rejects_an_all_blank_fingerprint(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset, _ = find_or_register_media_asset(session, source_url=REEL)
        with pytest.raises(BlankDownloadError):
            mark_media_asset_downloaded(
                asset,
                original_download_path="data/media/AbC123xyz.mp4",
                # Big enough to clear the size floor: only the fingerprint can
                # catch a placeholder that is padded out or re-encoded.
                file_size_bytes=2_000_000,
                content_hash=",".join(["0" * 16] * 5),
            )
        assert asset.download_status == "pending"


def test_mark_downloaded_accepts_real_footage_that_has_one_blank_frame(tmp_path) -> None:
    """A dark cut at one sample position is normal; only ALL-blank is a stub."""
    init_db()
    with get_session() as session:
        asset, _ = find_or_register_media_asset(session, source_url=REEL)
        mark_media_asset_downloaded(
            asset,
            original_download_path="data/media/AbC123xyz.mp4",
            file_size_bytes=2_000_000,
            content_hash=",".join(["0" * 16, "a1b2c3d4e5f60718", "0" * 16]),
        )
        assert asset.download_status == "downloaded"


def test_mark_downloaded_allows_unknown_size_and_fingerprint(tmp_path) -> None:
    """Registration paths that never stat or fingerprint must stay usable."""
    init_db()
    with get_session() as session:
        asset, _ = find_or_register_media_asset(session, source_url=REEL)
        mark_media_asset_downloaded(
            asset,
            original_download_path="data/media/AbC123xyz.mp4",
            file_size_bytes=None,
            content_hash=None,
        )
        assert asset.download_status == "downloaded"


def test_size_floor_sits_below_real_footage() -> None:
    """The smallest genuine asset on record is ~111 KB; the stub is 2.4 KB."""
    assert 2479 < MIN_PLAUSIBLE_VIDEO_BYTES < 111_522


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
