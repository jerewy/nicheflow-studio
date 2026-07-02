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


def test_only_explicit_repost_draft_overrides_historical_post() -> None:
    account_id = _make_account()
    untouched = _make_item(account_id=account_id)
    reopened = _make_item(account_id=account_id)
    with get_session() as session:
        session.get(DownloadItem, untouched).status = "pending_review"
        session.get(DownloadItem, reopened).status = "exported"
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=untouched,
                processed_path=f"C:/{untouched}.mp4",
                status="draft",
            )
        )
        session.flush()
        for item_id in (untouched, reopened):
            session.add(
                UploadJob(
                    account_id=account_id,
                    download_item_id=item_id,
                    processed_path=f"C:/{item_id}.mp4",
                    status="posted",
                )
            )
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=reopened,
                processed_path=f"C:/{reopened}.mp4",
                status="draft",
            )
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[untouched] == "posted"
    assert by_id[reopened] == "exported"


def test_status_derivation_scheduled_outranks_exported() -> None:
    account_id = _make_account()
    scheduled = _make_item(account_id=account_id)
    with get_session() as session:
        session.get(DownloadItem, scheduled).processed_path = "C:/out.mp4"
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=scheduled,
                processed_path="C:/out.mp4",
                status="scheduled",
            )
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[scheduled] == "scheduled"


def test_status_derivation_scheduled_outranks_pending_review() -> None:
    # Auto-distributed clips are exported + scheduled in the background while
    # their download_items.status is still 'pending_review'. The live schedule
    # must win so the table shows "Scheduled", not "Pending review".
    account_id = _make_account()
    item = _make_item(account_id=account_id)
    with get_session() as session:
        row = session.get(DownloadItem, item)
        row.status = "pending_review"
        row.processed_path = "C:/out.mp4"
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=item,
                processed_path="C:/out.mp4",
                status="scheduled",
            )
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[item] == "scheduled"


def test_status_derivation_cloud_job() -> None:
    # A job handed off to the Cloudflare Worker (status 'cloud') reads as "Cloud"
    # in the table, ranked like 'scheduled' (above pending_review).
    from nicheflow_studio.db.models import UploadJob

    account_id = _make_account()
    item = _make_item(account_id=account_id)
    with get_session() as session:
        row = session.get(DownloadItem, item)
        row.status = "pending_review"
        row.processed_path = "C:/out.mp4"
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=item,
                processed_path="C:/out.mp4",
                status="cloud",
            )
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[item] == "cloud"


def test_status_derivation_cloud_outranks_stale_scheduled_job() -> None:
    account_id = _make_account()
    item = _make_item(account_id=account_id)
    with get_session() as session:
        session.get(DownloadItem, item).processed_path = "C:/out.mp4"
        session.add_all(
            [
                UploadJob(
                    account_id=account_id,
                    download_item_id=item,
                    processed_path="C:/old.mp4",
                    status="scheduled",
                ),
                UploadJob(
                    account_id=account_id,
                    download_item_id=item,
                    processed_path="C:/out.mp4",
                    status="cloud",
                ),
            ]
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[item] == "cloud"


def test_status_derivation_posted_outranks_scheduled() -> None:
    # A scheduled row that has since posted (e.g. a stale schedule left behind)
    # must read as posted, not scheduled.
    account_id = _make_account()
    item = _make_item(account_id=account_id)
    with get_session() as session:
        session.get(DownloadItem, item).processed_path = "C:/out.mp4"
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=item,
                processed_path="C:/out.mp4",
                status="scheduled",
            )
        )
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=item,
                processed_path="C:/out.mp4",
                status="posted",
            )
        )
        session.commit()

    by_id = {r["id"]: r["status"] for r in library.list_items(account_id=account_id)}
    assert by_id[item] == "posted"


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


def test_pending_review_download_never_uses_publishing_account_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account-safety: the download must NEVER authenticate as the clip's own
    (publishing) account — yt-dlp fetching reels as a real account is what gets it
    flagged for automation. The cookie source is resolved WITHOUT the clip's
    profile; best_instagram_yt_dlp_cookiefile picks a sourcing account instead."""
    with get_session() as session:
        account = Account(name="A", niche="history", instagram_profile="history_acct")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://instagram.com/reel/z/",
                video_id="z",
                account_id=account.id,
                status="pending_review",
                review_state="pending_review",
            )
        )
        session.commit()
        item_id = session.query(DownloadItem).one().id

    monkeypatch.setattr(library, "safe_video_fingerprint", lambda _p: "h")
    seen: dict[str, object] = {}

    def fake_cookiefile(preferred_profile=None):
        seen["preferred_profile"] = preferred_profile
        return "cookie::sourcing"

    monkeypatch.setattr(library, "best_instagram_yt_dlp_cookiefile", fake_cookiefile)

    def fake_download(*, url, output_dir, cookiefile=None):
        seen["cookiefile"] = cookiefile
        path = tmp_path / "z.mp4"
        path.write_bytes(b"video")
        return SimpleNamespace(file_path=path)

    monkeypatch.setattr(library, "download_instagram_url", fake_download)

    library.ensure_item_downloaded(item_id)

    # The clip's own account profile must never be handed to the cookie resolver.
    assert seen["preferred_profile"] is None
    assert seen["cookiefile"] == "cookie::sourcing"


def test_download_persists_content_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The webview download path must compute and store the perceptual
    fingerprint. Regression: mark_media_asset_downloaded was called without
    content_hash, so every webview-downloaded asset kept content_hash=NULL and
    cross-repost footage dedup never matched."""
    source_url = "https://instagram.com/reel/fp/"
    with get_session() as session:
        account = Account(name="A", niche="history")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url=source_url,
                video_id="fp",
                account_id=account.id,
                status="pending_review",
                review_state="pending_review",
            )
        )
        session.commit()
        item_id = session.query(DownloadItem).one().id

    # Stub fingerprinting: the fake file isn't a real video, so exercise the
    # wiring (compute -> store) with a known hash instead of decoding frames.
    monkeypatch.setattr(library, "safe_video_fingerprint", lambda _path: "deadbeef,c0ffee")

    def fake_download(*, url, output_dir):
        path = tmp_path / "fp.mp4"
        path.write_bytes(b"video")
        return SimpleNamespace(file_path=path)

    result = library.ensure_item_downloaded(item_id, downloader=fake_download)
    assert result["downloaded"] is True

    with get_session() as session:
        asset = session.scalars(select(MediaAsset)).one()
        assert asset.content_hash == "deadbeef,c0ffee"


def test_deleted_source_raises_clean_reject_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A removed/private Instagram post should surface as a clear, rejectable
    message — not a generic 'Unexpected error' from a raw yt-dlp DownloadError.
    A gone source never recovers, so it must fail on the first attempt without
    burning the retry budget."""
    item_id = _make_item(file_path=None)
    monkeypatch.setattr(library.time, "sleep", lambda _seconds: None)
    calls = 0

    def gone_download(*, url, output_dir):
        nonlocal calls
        calls += 1
        raise RuntimeError(
            "ERROR: [Instagram] ABC: Instagram sent an empty media response."
        )

    with pytest.raises(library.SourceUnavailableError) as excinfo:
        library.ensure_item_downloaded(item_id, downloader=gone_download)
    assert "no longer available" in str(excinfo.value).lower() or "deleted" in str(
        excinfo.value
    ).lower()
    # Stays a ServiceError so the bridge shows the message verbatim.
    assert isinstance(excinfo.value, LibraryError)
    assert calls == 1  # no retry — the source won't come back


def test_transient_download_failure_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient blip (reset/timeout/rate-limit) on the first attempt should be
    retried, not surfaced to the user, so the clip opens on a later try."""
    item_id = _make_item(file_path=None)
    monkeypatch.setattr(library.time, "sleep", lambda _seconds: None)
    calls = 0

    def flaky_then_ok(*, url, output_dir):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("Connection reset by peer")
        path = tmp_path / "ok.mp4"
        path.write_bytes(b"video")
        return SimpleNamespace(file_path=path)

    result = library.ensure_item_downloaded(item_id, downloader=flaky_then_ok)
    assert result["downloaded"] is True
    assert calls == 3  # failed twice, succeeded on the third attempt


def test_generic_download_failure_is_translated_not_unexpected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other download failures (network, etc.) become a clean LibraryError rather
    than leaking a raw exception that the bridge would report generically — after
    exhausting the retry budget."""
    item_id = _make_item(file_path=None)
    monkeypatch.setattr(library.time, "sleep", lambda _seconds: None)
    calls = 0

    def flaky_download(*, url, output_dir):
        nonlocal calls
        calls += 1
        raise RuntimeError("Connection reset by peer")

    with pytest.raises(LibraryError) as excinfo:
        library.ensure_item_downloaded(item_id, downloader=flaky_download)
    assert not isinstance(excinfo.value, library.SourceUnavailableError)
    assert "instagram" in str(excinfo.value).lower()
    assert calls == library._DOWNLOAD_ATTEMPTS  # retried before giving up


def test_rate_limit_or_login_required_tells_user_to_relogin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instagram's 'rate-limit reached or login required' is a session problem, not
    a dead clip — surface a re-login/wait message immediately (no retry, no 'reject
    the clip' advice)."""
    item_id = _make_item(file_path=None)
    monkeypatch.setattr(library.time, "sleep", lambda _seconds: None)
    calls = 0

    def blocked_download(*, url, output_dir):
        nonlocal calls
        calls += 1
        raise RuntimeError(
            "ERROR: [Instagram] DYha3zWNJo2: Requested content is not available, "
            "rate-limit reached or login required."
        )

    with pytest.raises(library.SessionExpiredError) as excinfo:
        library.ensure_item_downloaded(item_id, downloader=blocked_download)
    message = str(excinfo.value).lower()
    assert "re-login" in message or "login" in message
    assert "reject" in message  # only as "don't reject" guidance
    assert calls == 1  # no retry — rate-limit won't clear within our backoff


def test_prefetch_warms_uncached_skips_cached_and_swallows_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Background prefetch downloads not-yet-cached clips, skips ones already on
    disk, and a single failing clip never aborts the batch or raises."""
    monkeypatch.setattr(library.time, "sleep", lambda _seconds: None)

    cached = tmp_path / "cached.mp4"
    cached.write_bytes(b"video")
    with get_session() as session:
        account = Account(name="A", niche="history")
        session.add(account)
        session.flush()
        session.add_all(
            [
                DownloadItem(
                    source_url="https://instagram.com/reel/p1",
                    video_id="p1",
                    account_id=account.id,
                    status="pending_review",
                    review_state="pending_review",
                ),
                DownloadItem(
                    source_url="https://instagram.com/reel/p2",
                    video_id="p2",
                    account_id=account.id,
                    status="pending_review",
                    review_state="pending_review",
                ),
                DownloadItem(
                    source_url="https://instagram.com/reel/p3",
                    video_id="p3",
                    account_id=account.id,
                    status="completed",
                    file_path=str(cached),
                ),
            ]
        )
        session.commit()
        ids = [r.id for r in session.scalars(select(DownloadItem)).all()]

    calls: list[str] = []

    def fake_download(*, url, output_dir):
        calls.append(url)
        if url.endswith("/p2"):
            raise RuntimeError("Connection reset by peer")
        path = tmp_path / "dl.mp4"
        path.write_bytes(b"video")
        return SimpleNamespace(file_path=path)

    result = library.prefetch_items(ids, downloader=fake_download)

    assert "https://instagram.com/reel/p1" in calls  # uncached → fetched
    assert "https://instagram.com/reel/p3" not in calls  # already on disk → skipped
    assert result["warmed"] == 1  # only p1 succeeded (p2 failed, swallowed)
    assert result["requested"] == 3


def test_concurrent_downloads_of_same_clip_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers racing on the same clip (a prefetch job vs the user's click)
    must serialize: only one yt-dlp run happens, the other reuses the finished
    file. Regression guard for the Windows WinError 32 / corrupt-merge race two
    concurrent downloads to the same output path caused."""
    import threading

    item_id = _make_item(file_path=None)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocking_download(*, url, output_dir):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()  # signal we're inside the download, holding the per-clip lock
        assert release.wait(2)
        path = tmp_path / "race.mp4"
        path.write_bytes(b"video")
        return SimpleNamespace(file_path=path)

    results: dict[str, dict] = {}

    def run(tag: str) -> None:
        results[tag] = library.ensure_item_downloaded(item_id, downloader=blocking_download)

    first = threading.Thread(target=run, args=("first",))
    first.start()
    assert entered.wait(2)  # first caller now holds the lock inside the downloader
    second = threading.Thread(target=run, args=("second",))
    second.start()
    release.set()  # let the first finish; the second can only proceed after it
    first.join(3)
    second.join(3)

    assert calls == 1  # one real download despite two concurrent callers
    assert results["first"]["downloaded"] is True
    assert results["second"]["downloaded"] is False  # reused the finished file
    assert results["first"]["file_path"] == results["second"]["file_path"]


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
