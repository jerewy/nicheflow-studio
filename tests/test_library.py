from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    DraftRevision,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
    UploadJob,
)
from nicheflow_studio.db.assignments import assignment_counts_by_account, distribute_niche
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import library
from nicheflow_studio.services.library import LibraryError


def _make_account(name: str = "Acc") -> int:
    with get_session() as session:
        account = Account(name=name, platform="instagram")
        session.add(account)
        session.commit()
        return account.id


def _make_item(*, account_id: int | None = None, file_path: str | None = "C:/x.mp4") -> int:
    with get_session() as session:
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Clip",
            file_path=file_path,
            status="completed",
            account_id=account_id,
        )
        session.add(item)
        session.commit()
        return item.id


def _pool_item_footage(source_url: str, video_id: str | None = None, niche: str = "history") -> int:
    """Register a media asset for ``source_url`` and accept it into a niche pool."""
    from nicheflow_studio.db.media_library import find_or_register_media_asset
    from nicheflow_studio.db.pools import accept_into_pool

    with get_session() as session:
        asset, _ = find_or_register_media_asset(
            session, source_url=source_url, shortcode=video_id, platform="instagram"
        )
        pool_item = accept_into_pool(session, media_asset=asset, niche=niche)
        session.commit()
        return pool_item.id


def test_list_items_includes_account_name_and_flags() -> None:
    account_id = _make_account("Movies")
    item_id = _make_item(account_id=account_id)

    rows = library.list_items()

    row = next(r for r in rows if r["id"] == item_id)
    assert row["account_name"] == "Movies"
    assert row["has_file"] is True
    assert row["has_processed"] is False
    # Freshly created item reads as "new" with the New flag set.
    assert row["status"] == "new"
    assert row["is_new"] is True


def test_list_items_filters_by_account() -> None:
    a = _make_account("A")
    b = _make_account("B")
    item_a = _make_item(account_id=a)
    _make_item(account_id=b)

    rows = library.list_items(account_id=a)

    assert [r["id"] for r in rows] == [item_a]


def test_list_items_assigns_per_account_sequence() -> None:
    a = _make_account("A")
    b = _make_account("B")
    # Interleave two accounts so global ids alternate; each account's "#N" must
    # ignore the other account's items.
    a1 = _make_item(account_id=a)
    _make_item(account_id=b)
    a2 = _make_item(account_id=a)
    _make_item(account_id=b)
    a3 = _make_item(account_id=a)

    seq = {r["id"]: r["account_seq"] for r in library.list_items(account_id=a)}

    assert seq == {a1: 1, a2: 2, a3: 3}
    # The newest item's number equals how many clips the account has.
    assert max(seq.values()) == 3


def test_per_account_sequence_skips_blocked_items() -> None:
    a = _make_account("A")
    first = _make_item(account_id=a)
    blocked = _make_item(account_id=a)
    last = _make_item(account_id=a)
    with get_session() as session:
        session.get(DownloadItem, blocked).review_state = "blocked"
        session.commit()

    seq = {r["id"]: r["account_seq"] for r in library.list_items(account_id=a)}

    # A blocked item is hidden and consumes no number, so the visible sequence
    # stays contiguous (no gap where the blocked clip was).
    assert blocked not in seq
    assert seq == {first: 1, last: 2}


def test_status_derivation_draft_exported_posted() -> None:
    from nicheflow_studio.db.models import UploadJob

    account_id = _make_account()
    draft = _make_item(account_id=account_id)
    exported = _make_item(account_id=account_id)
    posted = _make_item(account_id=account_id)
    with get_session() as session:
        session.get(DownloadItem, draft).title_draft = "A title"
        session.get(DownloadItem, exported).processed_path = "C:/out.mp4"
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=posted,
                processed_path="C:/posted.mp4",
                status="posted",
            )
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[draft] == "draft"
    assert by_id[exported] == "exported"
    assert by_id[posted] == "posted"


def test_status_derivation_failed_publish_outranks_exported() -> None:
    account_id = _make_account()
    failed = _make_item(account_id=account_id)
    with get_session() as session:
        session.get(DownloadItem, failed).processed_path = "C:/out.mp4"
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=failed,
                processed_path="C:/out.mp4",
                status="failed",
                error_message="not logged in",
            )
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[failed] == "failed"


def test_assign_and_clear_account() -> None:
    account_id = _make_account()
    item_id = _make_item(account_id=None)

    assigned = library.assign_account(item_id, account_id)
    assert assigned["account_id"] == account_id

    cleared = library.assign_account(item_id, None)
    assert cleared["account_id"] is None


def test_assign_unknown_account_raises() -> None:
    item_id = _make_item()
    with pytest.raises(LibraryError):
        library.assign_account(item_id, 99999)


def test_remove_item_cleans_dependents() -> None:
    account_id = _make_account()
    item_id = _make_item(account_id=account_id)
    with get_session() as session:
        session.add(
            ScrapeCandidate(
                scrape_source_url="https://s",
                source_url="https://x",
                state="queued",
                queued_download_item_id=item_id,
                account_id=account_id,
            )
        )
        session.add(
            DraftRevision(
                download_item_id=item_id,
                revision_number=1,
                title_options='["t1"]',
                caption_options='["c1"]',
            )
        )
        session.add(
            UploadJob(account_id=account_id, download_item_id=item_id, processed_path="C:/o.mp4")
        )
        session.commit()

    result = library.remove_item(item_id)

    assert result["removed_item_id"] == item_id
    assert result["deleted_revisions"] == 1
    with get_session() as session:
        assert session.get(DownloadItem, item_id) is None
        candidate = session.scalars(select(ScrapeCandidate)).first()
        assert candidate.queued_download_item_id is None
        assert candidate.state == "candidate"
        job = session.scalars(select(UploadJob)).first()
        assert job.download_item_id is None


def test_remove_item_from_pool_marks_removed() -> None:
    from nicheflow_studio.db.models import PoolItem
    from nicheflow_studio.db.pools import POOL_STATUS_REMOVED

    account_id = _make_account()
    item_id = _make_item(account_id=account_id)  # source_url .../reel/abc
    pool_item_id = _pool_item_footage("https://instagram.com/reel/abc")

    result = library.remove_item_from_pool(item_id)

    assert result["removed_pool_items"] == 1
    with get_session() as session:
        assert session.get(PoolItem, pool_item_id).acceptance_status == POOL_STATUS_REMOVED


def test_remove_item_from_pool_without_pool_is_noop() -> None:
    account_id = _make_account()
    item_id = _make_item(account_id=account_id)

    result = library.remove_item_from_pool(item_id)

    assert result["removed_pool_items"] == 0


def test_reject_item_rejects_candidate_and_removes_from_pool() -> None:
    from nicheflow_studio.db.models import PoolItem
    from nicheflow_studio.db.pools import POOL_STATUS_REMOVED

    account_id = _make_account()
    item_id = _make_item(account_id=account_id)
    pool_item_id = _pool_item_footage("https://instagram.com/reel/abc")
    with get_session() as session:
        session.add(
            ScrapeCandidate(
                scrape_source_url="https://s",
                source_url="https://instagram.com/reel/abc",
                state="downloaded",
                queued_download_item_id=item_id,
                account_id=account_id,
            )
        )
        session.commit()

    result = library.reject_item(item_id, "wrong_niche")

    assert result["rejected_candidates"] == 1
    assert result["removed_pool_items"] == 1
    assert result["review_state"] == "rejected"
    with get_session() as session:
        candidate = session.scalars(select(ScrapeCandidate)).first()
        assert candidate.state == "rejected_wrong_niche"
        assert session.get(PoolItem, pool_item_id).acceptance_status == POOL_STATUS_REMOVED
        assert session.get(DownloadItem, item_id).review_state == "rejected"


def test_reject_item_unknown_reason_raises() -> None:
    account_id = _make_account()
    item_id = _make_item(account_id=account_id)
    with pytest.raises(LibraryError):
        library.reject_item(item_id, "nope")


def test_pending_review_reject_releases_assignment_without_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        asset = MediaAsset(canonical_source_url="https://instagram.com/reel/reject/", source_shortcode="reject")
        session.add_all([account, asset])
        session.flush()
        pool_item = PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
        session.add(pool_item)
        session.flush()
        replacement_asset = MediaAsset(
            canonical_source_url="https://instagram.com/reel/replacement/",
            source_shortcode="replacement",
        )
        session.add(replacement_asset)
        session.flush()
        session.add(
            PoolItem(
                media_asset_id=replacement_asset.id,
                niche="history",
                acceptance_status="accepted",
            )
        )
        assignment = Assignment(pool_item_id=pool_item.id, account_id=account.id, niche="history")
        item = DownloadItem(
            source_url=asset.canonical_source_url,
            video_id=asset.source_shortcode,
            account_id=account.id,
            status="pending_review",
            review_state="pending_review",
        )
        session.add_all([assignment, item])
        session.commit()
        item_id = item.id
        assignment_id = assignment.id
        account_id = account.id
    monkeypatch.setattr(
        "nicheflow_studio.services.library.download_instagram_url",
        lambda **_kwargs: pytest.fail("reject must not download"),
    )

    result = library.reject_item(item_id, "low_quality")

    assert result["released_assignments"] == 1
    with get_session() as session:
        assert session.get(Assignment, assignment_id).status == "rejected"
        assert session.get(DownloadItem, item_id).file_path is None
        assert assignment_counts_by_account(session, "history").get(account_id, 0) == 0
        assert len(distribute_niche(session, "history", max_per_account=1)) == 1


def test_pending_review_first_use_downloads_once_and_reuses_globally(tmp_path: Path) -> None:
    source_url = "https://instagram.com/reel/lazy/"
    with get_session() as session:
        accounts = [Account(name="A", niche="history"), Account(name="B", niche="history")]
        session.add_all(accounts)
        session.flush()
        for account in accounts:
            session.add(
                DownloadItem(
                    source_url=source_url,
                    video_id="lazy",
                    account_id=account.id,
                    status="pending_review",
                    review_state="pending_review",
                )
            )
        session.commit()
        item_ids = [row.id for row in session.query(DownloadItem).all()]
    calls: list[str] = []

    def fake_download(*, url, output_dir):
        calls.append(url)
        path = tmp_path / "lazy.mp4"
        path.write_bytes(b"video")
        return SimpleNamespace(file_path=path)

    first = library.ensure_item_downloaded(item_ids[0], downloader=fake_download)
    second = library.ensure_item_downloaded(item_ids[1], downloader=fake_download)

    assert first["downloaded"] is True
    assert second["downloaded"] is False
    assert calls == [source_url]


def _row_for(account_id: int, item_id: int):
    return next(r for r in library.list_items(account_id=account_id) if r["id"] == item_id)


def test_mark_seen_clears_new_flag() -> None:
    account_id = _make_account()
    item_id = _make_item(account_id=account_id)

    assert _row_for(account_id, item_id)["is_new"] is True

    library.mark_seen(item_id)

    assert _row_for(account_id, item_id)["is_new"] is False


def test_reject_item_globally_blocks_hides_and_drops_assignments() -> None:
    from nicheflow_studio.db.blocklist import is_blocked
    from nicheflow_studio.db.models import Assignment, PoolItem
    from nicheflow_studio.db.pools import POOL_STATUS_REMOVED

    account_id = _make_account()
    item_id = _make_item(account_id=account_id)  # source .../reel/abc
    pool_item_id = _pool_item_footage("https://instagram.com/reel/abc")
    with get_session() as session:
        session.add(
            Assignment(
                pool_item_id=pool_item_id,
                account_id=account_id,
                niche="history",
                status="assigned",
            )
        )
        session.commit()

    result = library.reject_item_globally(item_id, "ad campaign")

    assert result["blocked"] is True
    assert result["removed_pool_items"] == 1
    assert result["dropped_assignments"] == 1
    # Hidden from the Processing list.
    assert all(r["id"] != item_id for r in library.list_items(account_id=account_id))
    with get_session() as session:
        assert is_blocked(session, source_url="https://instagram.com/reel/abc") is True
        assert session.get(PoolItem, pool_item_id).acceptance_status == POOL_STATUS_REMOVED
        assert session.query(Assignment).count() == 0
