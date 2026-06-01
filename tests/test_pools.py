from __future__ import annotations

import pytest

from nicheflow_studio.db.media_library import find_or_register_media_asset
from nicheflow_studio.db.pools import (
    CrossNicheError,
    accept_into_pool,
    pool_items_for_niche,
    pool_size,
)
from nicheflow_studio.db.session import get_session, init_db


def _asset(session, shortcode: str):
    asset, _ = find_or_register_media_asset(
        session, source_url=f"https://www.instagram.com/reel/{shortcode}/"
    )
    return asset


def test_accept_into_pool_creates_item(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset = _asset(session, "AAA111")
        item = accept_into_pool(
            session, media_asset=asset, niche="history", accepted_reason="good clip"
        )
        session.commit()

        assert item.niche == "history"
        assert item.acceptance_status == "accepted"
        assert item.accepted_at is not None
        assert item.media_asset_id == asset.id


def test_accept_is_idempotent_within_niche(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset = _asset(session, "BBB222")
        first = accept_into_pool(session, media_asset=asset, niche="history")
        second = accept_into_pool(session, media_asset=asset, niche="history")
        session.commit()

        assert first.id == second.id
        assert pool_size(session, "history") == 1


def test_cross_niche_accept_is_blocked_by_default(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset = _asset(session, "CCC333")
        accept_into_pool(session, media_asset=asset, niche="history")

        # Same asset into the movie pool must be refused — keeps niches isolated.
        with pytest.raises(CrossNicheError):
            accept_into_pool(session, media_asset=asset, niche="movie")


def test_cross_niche_accept_allowed_with_explicit_override(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset = _asset(session, "DDD444")
        accept_into_pool(session, media_asset=asset, niche="history")
        movie_item = accept_into_pool(
            session, media_asset=asset, niche="movie", allow_cross_niche=True
        )
        session.commit()

        assert movie_item.niche == "movie"
        assert pool_size(session, "history") == 1
        assert pool_size(session, "movie") == 1


def test_invalid_niche_rejected(tmp_path) -> None:
    init_db()
    with get_session() as session:
        asset = _asset(session, "EEE555")
        with pytest.raises(ValueError):
            accept_into_pool(session, media_asset=asset, niche="memes")


def test_pool_items_for_niche_isolates_by_niche(tmp_path) -> None:
    init_db()
    with get_session() as session:
        accept_into_pool(session, media_asset=_asset(session, "H1"), niche="history")
        accept_into_pool(session, media_asset=_asset(session, "H2"), niche="history")
        accept_into_pool(session, media_asset=_asset(session, "M1"), niche="movie")
        session.commit()

        history = pool_items_for_niche(session, "history")
        movie = pool_items_for_niche(session, "movie")

        assert {p.niche for p in history} == {"history"}
        assert len(history) == 2
        assert len(movie) == 1
