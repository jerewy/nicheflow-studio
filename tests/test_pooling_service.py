from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nicheflow_studio.db import assignments as assignments_db
from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
)
from nicheflow_studio.db.pool_intake import ReelMetadata, add_reel_to_pool
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import pooling
from nicheflow_studio.services.pooling import PoolingError


@pytest.fixture(autouse=True)
def fake_pool_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep distribution tests offline while materializing realistic local files.

    Distribution now fetches footage on a daemon thread after assigning; running
    that step inline keeps assertions deterministic (no thread joins) while still
    exercising the real download + pending-item file_path backfill path.
    """

    def download(*, url, output_dir):
        shortcode = url.rstrip("/").rsplit("/", 1)[-1]
        path = tmp_path / f"{shortcode}.mp4"
        path.write_bytes(b"video")
        return SimpleNamespace(file_path=path)

    monkeypatch.setattr(pooling, "download_instagram_url", download)
    monkeypatch.setattr(pooling, "_start_background_download", pooling._download_pool_assets)


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


def test_distribute_clip_downloads_pending_asset_before_assignment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    account_a, _account_b, pool_item_id = _seed_clip_with_accounts()
    with get_session() as session:
        pool_item = session.get(PoolItem, pool_item_id)
        asset = session.get(MediaAsset, pool_item.media_asset_id)
        asset.download_status = "pending"
        asset.original_download_path = None
        session.commit()

    downloaded = tmp_path / "clip.mp4"
    downloaded.write_bytes(b"video")
    monkeypatch.setattr(
        pooling,
        "download_instagram_url",
        lambda **_kwargs: SimpleNamespace(file_path=downloaded),
    )

    result = pooling.distribute_clip(pool_item_id, [account_a])

    assert result["assigned"] == 1
    with get_session() as session:
        pool_item = session.get(PoolItem, pool_item_id)
        asset = session.get(MediaAsset, pool_item.media_asset_id)
        item = session.query(DownloadItem).one()
        assert asset.download_status == "downloaded"
        assert asset.original_download_path == str(downloaded)
        assert item.file_path == str(downloaded)


def test_distribute_clip_does_not_assign_when_download_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_a, _account_b, pool_item_id = _seed_clip_with_accounts()
    with get_session() as session:
        pool_item = session.get(PoolItem, pool_item_id)
        asset = session.get(MediaAsset, pool_item.media_asset_id)
        asset.download_status = "pending"
        asset.original_download_path = None
        session.commit()

    def fail_download(**_kwargs):
        raise RuntimeError("Read timed out while fetching media")

    monkeypatch.setattr(pooling, "download_instagram_url", fail_download)

    with pytest.raises(PoolingError, match="Could not download"):
        pooling.distribute_clip(pool_item_id, [account_a])

    with get_session() as session:
        assert session.query(Assignment).count() == 0
        assert session.query(DownloadItem).count() == 0
        # Transient failure: the clip stays in the pool for a later retry.
        assert session.get(PoolItem, pool_item_id).acceptance_status == "accepted"


def test_distribute_clip_retires_pool_item_when_source_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted/private source is permanent: the clip leaves the pool (and its
    asset is blocklisted) instead of failing again on every distribute."""
    account_a, _account_b, pool_item_id = _seed_clip_with_accounts()
    with get_session() as session:
        pool_item = session.get(PoolItem, pool_item_id)
        asset = session.get(MediaAsset, pool_item.media_asset_id)
        asset.download_status = "pending"
        asset.original_download_path = None
        session.commit()

    def fail_download(**_kwargs):
        raise RuntimeError("Instagram sent an empty media response")

    monkeypatch.setattr(pooling, "download_instagram_url", fail_download)

    with pytest.raises(PoolingError, match="original reel is gone"):
        pooling.distribute_clip(pool_item_id, [account_a])

    with get_session() as session:
        assert session.query(Assignment).count() == 0
        assert session.get(PoolItem, pool_item_id).acceptance_status == "removed"
        pool_item = session.get(PoolItem, pool_item_id)
        asset = session.get(MediaAsset, pool_item.media_asset_id)
        assert asset.download_status == "unavailable"


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


def test_newly_pooled_reel_starts_pending_review() -> None:
    with get_session() as session:
        session.add(Account(name="History", platform="instagram", niche="history"))
        session.commit()

        result = add_reel_to_pool(
            session,
            niche="history",
            metadata=ReelMetadata(
                source_url="https://instagram.com/reel/pending/",
                shortcode="pending",
                channel_name="source",
                title="Pending clip",
                like_count=100,
            ),
        )
        session.commit()

        item = session.query(PoolItem).one()
        assert result.status == "added"
        assert item.acceptance_status == "pending_review"
        assert item.accepted_at is None


def test_pending_review_clip_is_not_unassigned_or_distributed() -> None:
    with get_session() as session:
        session.add(Account(name="History", platform="instagram", niche="history"))
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/pending/",
            source_shortcode="pending",
            download_status="downloaded",
        )
        session.add(asset)
        session.flush()
        session.add(
            PoolItem(
                media_asset_id=asset.id,
                niche="history",
                acceptance_status="pending_review",
            )
        )
        session.commit()

    assert pooling._unassigned_pool_item_ids("history") == []
    assert pooling.distribute_niche("history", max_per_account=1)["assigned"] == 0
    with get_session() as session:
        assert session.query(Assignment).count() == 0


def test_approve_pool_items_makes_clip_distributable() -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/approve/",
            source_shortcode="approve",
            download_status="downloaded",
        )
        session.add_all([account, asset])
        session.flush()
        pool_item = PoolItem(
            media_asset_id=asset.id,
            niche="history",
            acceptance_status="pending_review",
        )
        session.add(pool_item)
        session.commit()
        pool_item_id = pool_item.id

    assert pooling.approve_pool_items([pool_item_id]) == {"approved": 1}
    result = pooling.distribute_niche("history", max_per_account=1)

    assert result["assigned"] == 1
    with get_session() as session:
        item = session.get(PoolItem, pool_item_id)
        assert item.acceptance_status == "accepted"
        assert item.accepted_reason == "approved in review"
        assert item.accepted_at is not None
        assert session.query(Assignment).count() == 1


def test_review_queue_ranks_by_source_er_and_exposes_advisory_topic_metadata() -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        session.add(account)
        session.flush()
        cases = [
            ("large", "A photographed appearance", 1_000_000, 50_000, 0),
            ("focused", "The last time they sang this song together", 10_000, 500, 50),
        ]
        ids: dict[str, int] = {}
        for shortcode, title, views, likes, comments in cases:
            url = f"https://instagram.com/reel/{shortcode}/"
            asset = MediaAsset(
                platform="instagram",
                canonical_source_url=url,
                source_shortcode=shortcode,
                download_status="downloaded",
            )
            session.add(asset)
            session.flush()
            pool_item = PoolItem(
                media_asset_id=asset.id,
                niche="history",
                acceptance_status="pending_review",
            )
            session.add(pool_item)
            session.flush()
            ids[shortcode] = pool_item.id
            session.add(
                ScrapeCandidate(
                    scrape_source_url=url,
                    source_url=url,
                    video_id=shortcode,
                    title=title,
                    account_id=account.id,
                    view_count=views,
                    like_count=likes,
                    comment_count=comments,
                    duration_seconds=25,
                )
            )
        session.commit()

    rows = pooling.review_queue("history")

    assert [row["pool_item_id"] for row in rows] == [ids["focused"], ids["large"]]
    assert rows[0]["source_er"] == pytest.approx(0.055)
    assert rows[0]["topic_tier"] == "S"
    assert rows[0]["suggested_action"] == "accept"
    assert rows[1]["topic_tier"] == "C"
    assert rows[1]["suggested_action"] == "reject"
    with get_session() as session:
        focused = session.get(PoolItem, ids["focused"])
        large = session.get(PoolItem, ids["large"])
        assert focused.topic_tag == "S"
        assert large.topic_tag == "C"
        assert focused.acceptance_status == "pending_review"
        assert large.acceptance_status == "pending_review"


def test_review_queue_exposes_rights_confidence() -> None:
    with get_session() as session:
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/rights/",
            source_shortcode="rights",
            download_status="downloaded",
        )
        session.add(asset)
        session.flush()
        pool_item = PoolItem(
            media_asset_id=asset.id,
            niche="history",
            acceptance_status="pending_review",
            rights_confidence="broadcast_sport",
        )
        session.add(pool_item)
        session.commit()

    rows = pooling.review_queue("history")

    assert len(rows) == 1
    assert rows[0]["rights_confidence"] == "broadcast_sport"


def test_review_queue_rights_confidence_defaults_to_none() -> None:
    with get_session() as session:
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/norights/",
            source_shortcode="norights",
            download_status="downloaded",
        )
        session.add(asset)
        session.flush()
        session.add(
            PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="pending_review")
        )
        session.commit()

    rows = pooling.review_queue("history")

    assert rows[0]["rights_confidence"] is None


def test_set_pool_item_rights_confidence_updates_review_row() -> None:
    with get_session() as session:
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/setrights/",
            source_shortcode="setrights",
            download_status="downloaded",
        )
        session.add(asset)
        session.flush()
        pool_item = PoolItem(
            media_asset_id=asset.id, niche="history", acceptance_status="pending_review"
        )
        session.add(pool_item)
        session.commit()
        pool_item_id = pool_item.id

    result = pooling.set_pool_item_rights_confidence(pool_item_id, "archival")
    assert result == {"pool_item_id": pool_item_id, "rights_confidence": "archival"}

    rows = pooling.review_queue("history")
    assert rows[0]["rights_confidence"] == "archival"


def test_set_pool_item_rights_confidence_invalid_value_raises_pooling_error() -> None:
    with get_session() as session:
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/badrights/",
            source_shortcode="badrights",
            download_status="downloaded",
        )
        session.add(asset)
        session.flush()
        pool_item = PoolItem(
            media_asset_id=asset.id, niche="history", acceptance_status="pending_review"
        )
        session.add(pool_item)
        session.commit()
        pool_item_id = pool_item.id

    with pytest.raises(PoolingError):
        pooling.set_pool_item_rights_confidence(pool_item_id, "not-a-real-value")


def test_set_pool_item_rights_confidence_missing_item_raises_pooling_error() -> None:
    with pytest.raises(PoolingError):
        pooling.set_pool_item_rights_confidence(999999, "archival")


def test_reject_pool_items_removes_from_review_and_distribution() -> None:
    with get_session() as session:
        session.add(Account(name="History", platform="instagram", niche="history"))
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://instagram.com/reel/reject/",
            source_shortcode="reject",
            download_status="downloaded",
        )
        session.add(asset)
        session.flush()
        pool_item = PoolItem(
            media_asset_id=asset.id,
            niche="history",
            acceptance_status="pending_review",
        )
        session.add(pool_item)
        session.commit()
        pool_item_id = pool_item.id

    assert len(pooling.review_queue("history")) == 1
    assert pooling.reject_pool_items([pool_item_id], "wrong niche") == {"rejected": 1}

    assert pooling.review_queue("history") == []
    assert pooling.distribute_niche("history", max_per_account=1)["assigned"] == 0
    with get_session() as session:
        item = session.get(PoolItem, pool_item_id)
        assert item.acceptance_status == "removed"
        assert item.accepted_reason == "wrong niche"
        assert session.query(Assignment).count() == 0


def test_direct_accepted_clip_still_distributes_for_grandfathering() -> None:
    _seed_history_network(accounts=1, clips=1)

    result = pooling.distribute_niche("history", max_per_account=1)

    assert result["assigned"] == 1


def test_distribute_niche_assigns_then_downloads_in_background(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Distribution assigns clips immediately and fetches footage afterwards: a
    clip whose download fails is still assigned (logged, retried later), so a bad
    clip never blocks or shrinks the distribution."""
    with get_session() as session:
        session.add(Account(name="History", platform="instagram", niche="history"))
        for shortcode in ("works", "fails"):
            asset = MediaAsset(
                platform="instagram",
                canonical_source_url=f"https://instagram.com/reel/{shortcode}",
                source_shortcode=shortcode,
                download_status="pending",
            )
            session.add(asset)
            session.flush()
            session.add(
                PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
            )
        session.commit()

    downloaded = tmp_path / "works.mp4"
    downloaded.write_bytes(b"video")

    def download(*, url, output_dir):
        if url.endswith("/fails"):
            raise RuntimeError("private post")
        return SimpleNamespace(file_path=downloaded)

    monkeypatch.setattr(pooling, "download_instagram_url", download)

    result = pooling.distribute_niche("history", max_per_account=2)

    # Both clips assigned up front; the download step is best-effort and never
    # reduces the assignment count.
    assert result["assigned"] == 2
    assert result["download_failures"] == 0
    with get_session() as session:
        assert session.query(Assignment).count() == 2
        works = session.query(MediaAsset).filter_by(source_shortcode="works").one()
        fails = session.query(MediaAsset).filter_by(source_shortcode="fails").one()
        # The clip that downloaded is materialized; the failing one is left for a
        # later retry (its assignment still stands).
        assert works.original_download_path == str(downloaded)
        assert fails.original_download_path is None


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


def test_distribute_niche_service_defaults_to_distribute_daily_target() -> None:
    _seed_history_network(accounts=1, clips=2)

    result = pooling.distribute_niche("history")

    # Default rolling backlog target when an account sets no explicit value.
    assert result["max_per_account"] == 5
    assert result["assigned"] == 2  # only two clips available to place


def test_distribute_niche_uses_each_accounts_distribute_daily_target() -> None:
    with get_session() as session:
        session.add_all(
            [
                Account(
                    name="Three Daily",
                    platform="instagram",
                    niche="history",
                    distribute_daily_target=3,
                ),
                Account(
                    name="Eight Daily",
                    platform="instagram",
                    niche="history",
                    distribute_daily_target=8,
                ),
                Account(
                    name="Default Daily",
                    platform="instagram",
                    niche="history",
                    distribute_daily_target=None,
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
    # Default Daily (no explicit value) falls back to the module default of 5.
    assert counts == {"Three Daily": 3, "Eight Daily": 8, "Default Daily": 5}
    assert targets == {"Three Daily": 3, "Eight Daily": 8, "Default Daily": 5}


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
        assert all(item.file_path is not None and Path(item.file_path).exists() for item in pending)


def test_distribute_reuses_media_already_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloaded = tmp_path / "no-fetch.mp4"
    downloaded.write_bytes(b"video")
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        asset = MediaAsset(
            canonical_source_url="https://instagram.com/reel/no-fetch/",
            download_status="downloaded",
            original_download_path=str(downloaded),
        )
        session.add_all([account, asset])
        session.flush()
        pool_item = PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
        session.add(pool_item)
        session.commit()
        pool_item_id, account_id = pool_item.id, account.id
    monkeypatch.setattr(pooling, "download_instagram_url", lambda **_kwargs: pytest.fail("must reuse"))

    result = pooling.distribute_clip(pool_item_id, [account_id])

    assert result["assigned"] == 1

def _make_profile_ready(monkeypatch, *ready_profiles: str) -> None:
    """Pretend the given browser profiles have a saved Instagram login.

    The readiness filter goes through ``local_health``, whose only filesystem
    leaf is ``_has_sessionid`` — patching that keeps the rest of the health
    logic real (mirrors tests/test_account_health.py).
    """
    monkeypatch.setattr(
        "nicheflow_studio.core.account_health._has_sessionid",
        lambda name: name in ready_profiles,
    )


def test_auto_top_up_refills_only_below_target(monkeypatch) -> None:
    from nicheflow_studio.db import assignments as assignments_db
    from nicheflow_studio.services import pooling

    with get_session() as session:
        account = Account(
            name="Hist",
            platform="instagram",
            niche="history",
            distribute_daily_target=15,
            instagram_profile="hist-main",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    _make_profile_ready(monkeypatch, "hist-main")
    calls: list[str] = []
    monkeypatch.setattr(
        pooling,
        "distribute_niche",
        lambda niche, **kwargs: calls.append(niche) or {"niche": niche, "assigned": 0},
    )

    # Backlog exactly at target (15): already topped up, no refill.
    monkeypatch.setattr(
        assignments_db, "assignment_counts_by_account", lambda session, niche: {account_id: 15}
    )
    assert pooling.auto_top_up(("history",)) == []
    assert calls == []

    # One below target: the niche is refilled via the ranked distribution.
    monkeypatch.setattr(
        assignments_db, "assignment_counts_by_account", lambda session, niche: {account_id: 14}
    )
    results = pooling.auto_top_up(("history",))
    assert calls == ["history"]
    assert results and results[0]["niche"] == "history"


def test_auto_top_up_skips_niches_without_accounts() -> None:
    from nicheflow_studio.services import pooling

    assert pooling.auto_top_up(("movie",)) == []


def _seed_topup_accounts_and_clips(clips: int) -> tuple[int, int, int]:
    """One publish-ready candidate + two unready accounts (no profile / never
    logged in), plus accepted unassigned pool clips. Returns the account ids
    (ready, no_profile, never_logged_in)."""
    with get_session() as session:
        ready = Account(
            name="Ready",
            platform="instagram",
            niche="history",
            daily_posts_target=1,
            instagram_profile="ready-profile",
        )
        no_profile = Account(name="No Profile", platform="instagram", niche="history")
        never_logged_in = Account(
            name="Never Logged In",
            platform="instagram",
            niche="history",
            instagram_profile="ghost-profile",
        )
        session.add_all([ready, no_profile, never_logged_in])
        for i in range(clips):
            asset = MediaAsset(
                platform="instagram",
                canonical_source_url=f"https://instagram.com/reel/topup{i}/",
                source_shortcode=f"topup{i}",
            )
            session.add(asset)
            session.flush()
            session.add(
                PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
            )
        session.commit()
        return ready.id, no_profile.id, never_logged_in.id


def test_auto_top_up_assigns_only_to_publish_ready_accounts(monkeypatch) -> None:
    """The repro: accounts with a profile name but no recorded login (and ones
    with no profile at all) must receive ZERO assignments from the top-up tick,
    while a ready account is stocked to its rolling distribute target."""
    ready_id, no_profile_id, ghost_id = _seed_topup_accounts_and_clips(clips=30)
    _make_profile_ready(monkeypatch, "ready-profile")

    results = pooling.auto_top_up(("history",))

    assert len(results) == 1
    assert results[0]["assigned"] == 5  # default distribute target, ready account only
    with get_session() as session:
        counts = assignments_db.assignment_counts_by_account(session, "history")
    assert counts.get(ready_id, 0) == 5
    assert counts.get(no_profile_id, 0) == 0
    assert counts.get(ghost_id, 0) == 0


def test_auto_top_up_skips_niche_when_no_account_is_publish_ready(monkeypatch) -> None:
    _seed_topup_accounts_and_clips(clips=5)
    _make_profile_ready(monkeypatch)  # no profile has a saved login

    assert pooling.auto_top_up(("history",)) == []
    with get_session() as session:
        assert assignments_db.assignment_counts_by_account(session, "history") == {}


def test_distribute_niche_ready_only_reports_reason(monkeypatch) -> None:
    _seed_topup_accounts_and_clips(clips=5)
    _make_profile_ready(monkeypatch)  # no profile has a saved login

    result = pooling.distribute_niche("history", publish_ready_only=True)

    assert result["assigned"] == 0
    assert result["reason"] == "no_ready_accounts"


def test_distribute_niche_manual_path_still_serves_unready_accounts(monkeypatch) -> None:
    """Pre-stocking an account before its first login stays an explicit user
    choice: the manual Distribute path keeps assigning regardless of readiness."""
    ready_id, no_profile_id, ghost_id = _seed_topup_accounts_and_clips(clips=30)
    _make_profile_ready(monkeypatch, "ready-profile")

    result = pooling.distribute_niche("history", max_per_account=2)

    assert result["assigned"] == 6  # 3 accounts x cap 2 — nobody filtered
    with get_session() as session:
        counts = assignments_db.assignment_counts_by_account(session, "history")
    assert counts == {ready_id: 2, no_profile_id: 2, ghost_id: 2}


def test_release_unpublishable_assignments_frees_locked_clips(monkeypatch) -> None:
    """Remediation for pre-fix top-up runs: 'assigned' rows booked onto
    accounts that can never publish are deleted together with their untouched
    pending_review items, the freed clips redistribute to a ready account, and
    posted history plus the ready account's own backlog stay intact."""
    ready_id, no_profile_id, ghost_id = _seed_topup_accounts_and_clips(clips=6)
    _make_profile_ready(monkeypatch, "ready-profile")

    # Recreate the pre-fix state via the unfiltered manual path: every account
    # (publishable or not) holds 2 clips, each with a pending_review item.
    pooling.distribute_niche("history", max_per_account=2)
    with get_session() as session:
        assert assignments_db.assignment_counts_by_account(session, "history") == {
            ready_id: 2,
            no_profile_id: 2,
            ghost_id: 2,
        }
        # One ghost assignment already posted — history, not a lock to release.
        posted = (
            session.query(Assignment).filter(Assignment.account_id == ghost_id).first()
        )
        posted.status = assignments_db.ASSIGNMENT_STATUS_POSTED
        posted_id = posted.id
        session.commit()

    # Dry run reports the exact release without writing anything.
    preview = pooling.release_unpublishable_assignments(dry_run=True)
    assert preview["dry_run"] is True
    assert preview["released_assignments"] == 3  # 2 no-profile + 1 ghost still assigned
    assert preview["deleted_pending_items"] == 3
    with get_session() as session:
        assert session.query(Assignment).count() == 6
        assert session.query(DownloadItem).filter(
            DownloadItem.status == "pending_review"
        ).count() == 6

    result = pooling.release_unpublishable_assignments()

    assert result["released_assignments"] == 3
    assert result["deleted_pending_items"] == 3
    assert {(row["account_id"], row["assignments"]) for row in result["accounts"]} == {
        (no_profile_id, 2),
        (ghost_id, 1),
    }
    with get_session() as session:
        # Posted history and the ready account's backlog survive.
        assert (
            session.get(Assignment, posted_id).status
            == assignments_db.ASSIGNMENT_STATUS_POSTED
        )
        remaining = session.query(Assignment).all()
        assert {(a.account_id, a.status) for a in remaining} == {
            (ready_id, "assigned"),
            (ghost_id, "posted"),
        }
        # Only the released assignments' pending items were deleted: the ready
        # account keeps its 2, the posted ghost assignment keeps its 1.
        pending_by_account = sorted(
            item.account_id
            for item in session.query(DownloadItem)
            .filter(DownloadItem.status == "pending_review")
            .all()
        )
        assert pending_by_account == sorted([ready_id, ready_id, ghost_id])

    # The freed clips are distributable again — and land on the ready account.
    redistribute = pooling.distribute_niche("history", publish_ready_only=True)

    assert redistribute["assigned"] == 3
    with get_session() as session:
        counts = assignments_db.assignment_counts_by_account(session, "history")
        assert counts == {ready_id: 5}  # 2 original + the 3 released clips
        # Every accepted clip is distributed: the posted one stays locked, the
        # released ones are re-booked on the ready account.
        assert len(assignments_db.assigned_pool_item_ids(session, "history")) == 6
        assert session.query(DownloadItem).filter(
            DownloadItem.status == "pending_review",
            DownloadItem.account_id == ready_id,
        ).count() == 5


def test_release_unpublishable_assignments_noop_when_all_ready(monkeypatch) -> None:
    ready_id, _no_profile_id, _ghost_id = _seed_topup_accounts_and_clips(clips=2)
    _make_profile_ready(monkeypatch, "ready-profile")
    pooling.distribute_niche("history", publish_ready_only=True)

    result = pooling.release_unpublishable_assignments()

    assert result["released_assignments"] == 0
    assert result["deleted_pending_items"] == 0
    assert result["accounts"] == []
    with get_session() as session:
        assert assignments_db.assignment_counts_by_account(session, "history") == {
            ready_id: 2
        }


def test_release_missing_media_assignments_frees_legacy_broken_clips(tmp_path: Path) -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        session.add(account)
        session.flush()

        missing_asset = MediaAsset(
            canonical_source_url="https://instagram.com/reel/missing/",
            source_shortcode="missing",
            download_status="pending",
        )
        progressed_asset = MediaAsset(
            canonical_source_url="https://instagram.com/reel/progressed/",
            source_shortcode="progressed",
            download_status="pending",
        )
        good_file = tmp_path / "good.mp4"
        good_file.write_bytes(b"video")
        good_asset = MediaAsset(
            canonical_source_url="https://instagram.com/reel/good/",
            source_shortcode="good",
            download_status="downloaded",
            original_download_path=str(good_file),
        )
        session.add_all([missing_asset, progressed_asset, good_asset])
        session.flush()

        missing_pool = PoolItem(
            media_asset_id=missing_asset.id, niche="history", acceptance_status="accepted"
        )
        progressed_pool = PoolItem(
            media_asset_id=progressed_asset.id, niche="history", acceptance_status="accepted"
        )
        good_pool = PoolItem(
            media_asset_id=good_asset.id, niche="history", acceptance_status="accepted"
        )
        session.add_all([missing_pool, progressed_pool, good_pool])
        session.flush()
        session.add_all(
            [
                Assignment(pool_item_id=missing_pool.id, account_id=account.id, niche="history"),
                Assignment(pool_item_id=progressed_pool.id, account_id=account.id, niche="history"),
                Assignment(pool_item_id=good_pool.id, account_id=account.id, niche="history"),
                DownloadItem(
                    source_url=missing_asset.canonical_source_url,
                    video_id="missing",
                    account_id=account.id,
                    status="pending_review",
                    review_state="pending_review",
                ),
                DownloadItem(
                    source_url=progressed_asset.canonical_source_url,
                    video_id="progressed",
                    account_id=account.id,
                    status="completed",
                    review_state="approved",
                ),
                DownloadItem(
                    source_url=good_asset.canonical_source_url,
                    video_id="good",
                    account_id=account.id,
                    file_path=str(good_file),
                    status="pending_review",
                    review_state="pending_review",
                ),
            ]
        )
        session.commit()
        account_id = account.id
        missing_pool_id = missing_pool.id
        progressed_pool_id = progressed_pool.id
        good_pool_id = good_pool.id

    preview = pooling.release_missing_media_assignments(dry_run=True)

    assert preview["released_assignments"] == 1
    assert preview["deleted_pending_items"] == 1
    with get_session() as session:
        assert session.query(Assignment).count() == 3
        assert session.query(DownloadItem).count() == 3

    result = pooling.release_missing_media_assignments()

    assert result["released_assignments"] == 1
    assert result["deleted_pending_items"] == 1
    with get_session() as session:
        remaining_pool_ids = {
            row.pool_item_id for row in session.query(Assignment).all()
        }
        assert remaining_pool_ids == {progressed_pool_id, good_pool_id}
        assert missing_pool_id not in assignments_db.assigned_pool_item_ids(session, "history")
        assert (
            session.query(DownloadItem)
            .filter(
                DownloadItem.account_id == account_id,
                DownloadItem.source_url == "https://instagram.com/reel/missing/",
            )
            .count()
            == 0
        )


def test_repair_pending_review_media_links_reuses_shared_download(tmp_path: Path) -> None:
    media_file = tmp_path / "shared.mp4"
    media_file.write_bytes(b"video")
    with get_session() as session:
        asset = MediaAsset(
            canonical_source_url="https://instagram.com/reel/shared/",
            source_shortcode="shared",
            download_status="downloaded",
            original_download_path=str(media_file),
        )
        item = DownloadItem(
            source_url=asset.canonical_source_url,
            video_id="shared",
            status="pending_review",
            review_state="pending_review",
        )
        session.add_all([asset, item])
        session.commit()
        item_id = item.id

    preview = pooling.repair_pending_review_media_links(dry_run=True)
    assert preview["repaired_items"] == 1
    with get_session() as session:
        assert session.get(DownloadItem, item_id).file_path is None

    result = pooling.repair_pending_review_media_links()
    assert result["repaired_items"] == 1
    with get_session() as session:
        assert session.get(DownloadItem, item_id).file_path == str(media_file)
