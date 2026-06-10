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
    assert clips[0]["acceptance_status"] == "accepted"
    assert "original_download_path" in clips[0]


def test_remove_and_restore_pool_item() -> None:
    _account_id, pool_item_id = _seed_history_pool()

    # Removing drops the clip from the default active-only view...
    pooling.remove_pool_item(pool_item_id)
    assert pooling.list_source_clips("history", "—") == []

    # ...but it stays visible (marked removed) when explicitly included.
    with_removed = pooling.list_source_clips("history", "—", include_removed=True)
    assert len(with_removed) == 1
    assert with_removed[0]["acceptance_status"] == "removed"

    # Restoring returns it to the active pool.
    pooling.restore_pool_item(pool_item_id)
    active = pooling.list_source_clips("history", "—")
    assert len(active) == 1
    assert active[0]["acceptance_status"] == "accepted"


def test_remove_pool_item_unknown_raises() -> None:
    with pytest.raises(PoolingError):
        pooling.remove_pool_item(999999)


def test_restore_pool_item_unknown_raises() -> None:
    with pytest.raises(PoolingError):
        pooling.restore_pool_item(999999)


def _seed_clip_with_accounts() -> tuple[int, int, int]:
    """Two history accounts + one accepted (unassigned) pool clip. Returns
    (account_a_id, account_b_id, pool_item_id)."""
    with get_session() as session:
        account_a = Account(name="Hist A", platform="instagram", niche="history")
        account_b = Account(name="Hist B", platform="instagram", niche="history")
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/zzz",
            source_shortcode="zzz",
            download_status="downloaded",
        )
        session.add_all([account_a, account_b, asset])
        session.commit()
        pool_item = PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
        session.add(pool_item)
        session.commit()
        return account_a.id, account_b.id, pool_item.id


def test_niche_accounts_lists_accounts() -> None:
    account_id, _ = _seed_history_pool()
    accounts = pooling.niche_accounts("history")
    assert any(a["id"] == account_id for a in accounts)


def test_distribute_clip_assigns_to_chosen_accounts() -> None:
    account_a, account_b, pool_item_id = _seed_clip_with_accounts()

    result = pooling.distribute_clip(pool_item_id, [account_a, account_b])
    assert result["assigned"] == 2

    # Idempotent: re-distributing to an account it already has adds nothing.
    assert pooling.distribute_clip(pool_item_id, [account_a])["assigned"] == 0

    clip = next(c for c in pooling.list_source_clips("history", "—") if c["pool_item_id"] == pool_item_id)
    assert "Hist A" in clip["distributed_to"]
    assert "Hist B" in clip["distributed_to"]


def test_distribute_clip_ignores_out_of_niche_accounts() -> None:
    with get_session() as session:
        movie = Account(name="Movie", platform="instagram", niche="movie")
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/qqq",
            source_shortcode="qqq",
            download_status="downloaded",
        )
        session.add_all([movie, asset])
        session.commit()
        pool_item = PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
        session.add(pool_item)
        session.commit()
        movie_id, pool_item_id = movie.id, pool_item.id

    # A movie account must not receive a history clip.
    assert pooling.distribute_clip(pool_item_id, [movie_id])["assigned"] == 0


def test_distribute_clip_unknown_item_raises() -> None:
    with pytest.raises(PoolingError):
        pooling.distribute_clip(999999, [1])


def _seed_history_network(*, accounts: int, clips: int) -> None:
    """N history accounts + M accepted, unassigned pool clips (no metadata)."""
    with get_session() as session:
        for i in range(accounts):
            session.add(Account(name=f"Net {i}", platform="instagram", niche="history"))
        for i in range(clips):
            asset = MediaAsset(
                platform="instagram",
                canonical_source_url=f"https://instagram.com/reel/net{i}",
                source_shortcode=f"net{i}",
                download_status="downloaded",
            )
            session.add(asset)
            session.flush()
            session.add(
                PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
            )
        session.commit()


def test_distribute_niche_service_assigns_and_summarizes() -> None:
    _seed_history_network(accounts=2, clips=10)

    result = pooling.distribute_niche("history", max_per_account=3)

    assert result["niche"] == "history"
    assert result["max_per_account"] == 3
    assert result["assigned"] == 6  # 2 accounts x cap 3
    assert sum(account["count"] for account in result["accounts"]) == 6
    assert all(account["account_name"] for account in result["accounts"])


def test_distribute_niche_service_defaults_to_target_backlog() -> None:
    _seed_history_network(accounts=1, clips=2)

    result = pooling.distribute_niche("history")

    assert result["max_per_account"] == 28  # target_backlog() MVP default
    assert result["assigned"] == 2  # only two clips available to place


def test_distribute_niche_service_rejects_unknown_niche() -> None:
    with pytest.raises(PoolingError):
        pooling.distribute_niche("not-a-niche")
