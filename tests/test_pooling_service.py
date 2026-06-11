from __future__ import annotations

import pytest

from nicheflow_studio.db import assignments as assignments_db
from nicheflow_studio.db.models import Account, Assignment, DownloadItem, MediaAsset, PoolItem
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


def test_distribute_niche_flags_pinned_assignment_even_at_cap() -> None:
    with get_session() as session:
        account = Account(name="Pinned Hist", platform="instagram", niche="history")
        session.add(account)
        session.flush()
        asset = MediaAsset(canonical_source_url="https://instagram.com/reel/regular/")
        session.add(asset)
        session.flush()
        session.add(PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted"))
        session.commit()
        account_id = account.id

    first = pooling.distribute_niche("history", max_per_account=1)
    with get_session() as session:
        asset = MediaAsset(canonical_source_url="https://instagram.com/reel/pinned/")
        session.add(asset)
        session.flush()
        session.add(
            PoolItem(
                media_asset_id=asset.id,
                niche="history",
                acceptance_status="accepted",
                pinned_account_id=account_id,
            )
        )
        session.commit()
    second = pooling.distribute_niche("history", max_per_account=1)

    assert first["assigned"] == 1
    assert first["pinned"] == 0
    assert second["assigned"] == 1
    assert second["pinned"] == 1
    assert second["accounts"][0]["pinned"] == 1


def test_distribute_niche_service_defaults_to_target_backlog() -> None:
    _seed_history_network(accounts=1, clips=2)

    result = pooling.distribute_niche("history")

    assert result["max_per_account"] == 28
    assert result["assigned"] == 2  # only two clips available to place


def test_distribute_niche_uses_each_accounts_daily_posts_target() -> None:
    with get_session() as session:
        session.add_all(
            [
                Account(
                    name="Three Daily",
                    platform="instagram",
                    niche="history",
                    daily_posts_target=3,
                ),
                Account(
                    name="Five Daily",
                    platform="instagram",
                    niche="history",
                    daily_posts_target=5,
                ),
                Account(
                    name="Default Daily",
                    platform="instagram",
                    niche="history",
                    daily_posts_target=None,
                ),
            ]
        )
        for index in range(100):
            asset = MediaAsset(
                platform="instagram",
                canonical_source_url=f"https://instagram.com/reel/cadence{index}",
                source_shortcode=f"cadence{index}",
                download_status="downloaded",
            )
            session.add(asset)
            session.flush()
            session.add(
                PoolItem(
                    media_asset_id=asset.id,
                    niche="history",
                    acceptance_status="accepted",
                )
            )
        session.commit()

    result = pooling.distribute_niche("history")

    counts = {row["account_name"]: row["count"] for row in result["accounts"]}
    targets = {row["account_name"]: row["target"] for row in result["accounts"]}
    assert result["max_per_account"] is None
    assert counts == {"Three Daily": 21, "Five Daily": 35, "Default Daily": 28}
    assert targets == {"Three Daily": 21, "Five Daily": 35, "Default Daily": 28}


def test_distribute_niche_service_rejects_unknown_niche() -> None:
    with pytest.raises(PoolingError):
        pooling.distribute_niche("not-a-niche")


def test_manual_distribute_selected_accounts_only_and_ignores_cadence_targets() -> None:
    with get_session() as session:
        accounts = [
            Account(name=f"A{i}", platform="instagram", niche="history", daily_posts_target=1)
            for i in range(3)
        ]
        session.add_all(accounts)
        session.flush()
        selected = {accounts[0].id: 2, accounts[2].id: 3}
        for i in range(10):
            asset = MediaAsset(
                canonical_source_url=f"https://instagram.com/reel/manual{i}/",
                source_shortcode=f"manual{i}",
            )
            session.add(asset)
            session.flush()
            session.add(PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted"))
        session.commit()

    result = pooling.distribute_niche_explicit("history", selected)

    assert result["assigned"] == 5
    assert {row["account_id"]: row["count"] for row in result["accounts"]} == selected
    with get_session() as session:
        counts = assignments_db.assignment_counts_by_account(session, "history")
        assert counts == selected
        pending = session.query(DownloadItem).filter(DownloadItem.status == "pending_review").all()
        assert len(pending) == 5
        assert all(item.file_path is None for item in pending)


def test_distribute_never_fetches_media(monkeypatch: pytest.MonkeyPatch) -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        asset = MediaAsset(canonical_source_url="https://instagram.com/reel/no-fetch/")
        session.add_all([account, asset])
        session.flush()
        pool_item = PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
        session.add(pool_item)
        session.commit()
        pool_item_id, account_id = pool_item.id, account.id
    monkeypatch.setattr(
        "nicheflow_studio.downloader.instagram.download_instagram_url",
        lambda **_kwargs: pytest.fail("distribution must not fetch media"),
    )

    result = pooling.distribute_clip(pool_item_id, [account_id])

    assert result["assigned"] == 1
