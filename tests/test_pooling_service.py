from __future__ import annotations

import pytest

from nicheflow_studio.db.models import Account, Assignment, MediaAsset, PoolItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import pooling
from nicheflow_studio.services.pooling import PoolingError


def _seed_history_pool() -> tuple[int, int]:
    """One history account + one accepted pool item assigned to it."""
    with get_session() as session:
        account = Account(name="Past Moments", platform="instagram", niche="history")
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/abc",
            source_shortcode="abc",
            download_status="downloaded",
        )
        session.add_all([account, asset])
        session.commit()
        pool_item = PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
        session.add(pool_item)
        session.commit()
        session.add(Assignment(pool_item_id=pool_item.id, account_id=account.id, niche="history"))
        session.commit()
        return account.id, pool_item.id


def test_overview_reports_both_niches() -> None:
    overview = pooling.overview()
    niches = {n["niche"] for n in overview["niches"]}
    assert niches == {"history", "movie"}


def test_overview_counts_pool_and_assignments() -> None:
    account_id, _ = _seed_history_pool()

    overview = pooling.overview()
    history = next(n for n in overview["niches"] if n["niche"] == "history")

    assert history["pooled"] == 1
    assert history["assigned"] == 1
    assert history["unused"] == 0
    assert history["assignments_by_account"][0]["account_id"] == account_id
    assert history["assignments_by_account"][0]["count"] == 1


def test_list_pool_items_returns_accepted_clip() -> None:
    _seed_history_pool()

    items = pooling.list_pool_items("history")

    assert len(items) == 1
    assert items[0]["clip_label"] == "abc"
    assert items[0]["is_distributed"] is True
    assert "Past Moments" in items[0]["distributed_to"]


def test_list_pool_items_unknown_niche_raises() -> None:
    with pytest.raises(PoolingError):
        pooling.list_pool_items("not-a-niche")


def test_source_drilldown_returns_source_and_clip_rows() -> None:
    _seed_history_pool()

    sources = pooling.list_sources("history")
    assert sources == [{"source_label": "—", "clip_count": 1, "newest_post_at": None}]

    clips = pooling.list_source_clips("history", "—")
    assert len(clips) == 1
    assert clips[0]["shortcode"] == "abc"
    assert clips[0]["download_status"] == "downloaded"
