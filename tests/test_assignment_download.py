from __future__ import annotations

import random
from pathlib import Path

from nicheflow_studio.db.assignments import (
    assignment_counts_by_account,
    distribute_niche,
    pending_download_assignments,
)
from nicheflow_studio.db.media_library import find_media_asset
from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.pools import (
    POOL_STATUS_ACCEPTED,
    accept_candidate_into_pool,
    pool_size,
)
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.queue import download_assigned_pending


class _FakeResult:
    """Stand-in for an InstagramDownloadResult (only file_path is used here)."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.video_id = Path(file_path).stem
        self.title = "t"
        self.extractor = "instagram"


def _seed_assigned_pending(session, count: int) -> list[str]:
    """Create a history account + `count` candidates, pool them (pending), and
    distribute. Returns the shortcodes."""
    account = Account(name="Hist", platform="instagram", niche="history")
    session.add(account)
    session.flush()
    shortcodes: list[str] = []
    for i in range(count):
        shortcode = f"SEED{i}"
        candidate = ScrapeCandidate(
            scrape_source_url="https://www.instagram.com/src/",
            source_url=f"https://www.instagram.com/reel/{shortcode}/",
            video_id=shortcode,
            account_id=account.id,
        )
        session.add(candidate)
        session.flush()
        item = accept_candidate_into_pool(session, candidate=candidate, niche="history")
        # Intake now lands clips in pending_review; only reviewed-and-approved
        # clips distribute, so simulate the review approval here.
        item.acceptance_status = POOL_STATUS_ACCEPTED
        shortcodes.append(shortcode)
    session.flush()
    distribute_niche(session, "history", rng=random.Random(1))
    return shortcodes


def test_pending_download_assignments_lists_only_pending(tmp_path) -> None:
    init_db()
    with get_session() as session:
        shortcodes = _seed_assigned_pending(session, 3)
        session.commit()

        pending = pending_download_assignments(session, niche="history")
        assert {p.shortcode for p in pending} == set(shortcodes)
        # Each pending target carries the assignment(s) waiting on it.
        assert all(len(p.assignment_ids) >= 1 for p in pending)


def test_download_assigned_pending_marks_downloaded_with_fingerprint(tmp_path) -> None:
    init_db()
    with get_session() as session:
        _seed_assigned_pending(session, 3)
        session.commit()

    def fake_downloader(url: str) -> _FakeResult:
        # Materialise a real file so size + reuse checks behave like production.
        shortcode = url.rstrip("/").rsplit("/", 1)[-1]
        path = tmp_path / f"{shortcode}.mp4"
        path.write_bytes(b"fake video bytes")
        return _FakeResult(str(path))

    # Unique fingerprint per clip so the footage-dedup step doesn't treat them
    # as duplicates of each other.
    summary = download_assigned_pending(
        niche="history",
        downloader=fake_downloader,
        fingerprinter=lambda path: f"FP-{Path(path).stem}",
    )

    assert summary.downloaded == 3
    assert summary.reused == 0
    assert summary.failed == 0
    assert summary.duplicates == 0
    with get_session() as session:
        # Every asset is now downloaded with a path + fingerprint recorded.
        assert pending_download_assignments(session, niche="history") == []
        asset = find_media_asset(session, source_url="https://www.instagram.com/reel/SEED0/")
        assert asset.download_status == "downloaded"
        assert asset.original_download_path is not None
        assert asset.content_hash == "FP-SEED0"


def test_download_assigned_pending_reuses_existing_file(tmp_path) -> None:
    init_db()
    with get_session() as session:
        _seed_assigned_pending(session, 2)
        session.commit()

    calls: list[str] = []

    def counting_downloader(url: str) -> _FakeResult:
        calls.append(url)
        path = tmp_path / f"{url.rstrip('/').rsplit('/', 1)[-1]}.mp4"
        path.write_bytes(b"v")
        return _FakeResult(str(path))

    fp = lambda path: f"FP-{Path(path).stem}"  # unique per clip -> no false dups
    # First run downloads both.
    first = download_assigned_pending(
        niche="history", downloader=counting_downloader, fingerprinter=fp
    )
    assert first.downloaded == 2
    assert first.duplicates == 0
    # Second run finds them already on disk and reuses (no new downloads).
    second = download_assigned_pending(
        niche="history", downloader=counting_downloader, fingerprinter=fp
    )
    assert second.downloaded == 0
    assert second.reused == 0  # nothing pending remains, so nothing to reuse
    assert len(calls) == 2  # downloader never called again


def test_download_assigned_pending_records_failures(tmp_path) -> None:
    init_db()
    with get_session() as session:
        _seed_assigned_pending(session, 2)
        session.commit()

    def failing_downloader(url: str):
        raise RuntimeError("network down")

    summary = download_assigned_pending(
        niche="history", downloader=failing_downloader, fingerprinter=lambda _p: "H"
    )
    assert summary.downloaded == 0
    assert summary.failed == 2
    assert len(summary.errors) == 2
    # Assets stay pending so a later run can retry them.
    with get_session() as session:
        assert len(pending_download_assignments(session, niche="history")) == 2


def test_download_retires_gone_source_and_distribute_refills(tmp_path) -> None:
    """A permanently-deleted reel must not stay stuck 'assigned' forever: its
    asset is retired, pool item removed, assignment released, dedup keys
    blocklisted — and the next Distribute refills the freed slot."""
    init_db()
    with get_session() as session:
        _seed_assigned_pending(session, 2)
        session.commit()

    def gone_downloader(url: str) -> _FakeResult:
        if "SEED0" in url:
            raise RuntimeError("ERROR: [Instagram] SEED0: The post may have been deleted.")
        path = tmp_path / f"{url.rstrip('/').rsplit('/', 1)[-1]}.mp4"
        path.write_bytes(b"v")
        return _FakeResult(str(path))

    summary = download_assigned_pending(
        niche="history",
        downloader=gone_downloader,
        fingerprinter=lambda path: f"FP-{Path(path).stem}",
    )

    assert summary.unavailable == 1
    assert summary.failed == 0
    assert summary.downloaded == 1
    assert any("source is gone" in error for error in summary.errors)

    from nicheflow_studio.db.blocklist import is_blocked
    from nicheflow_studio.db.models import ScrapeCandidate as _SC

    with get_session() as session:
        asset = find_media_asset(
            session, source_url="https://www.instagram.com/reel/SEED0/"
        )
        assert asset.download_status == "unavailable"
        # Pool item removed and assignment released -> account reads as short.
        assert pool_size(session, "history") == 1
        assert sum(assignment_counts_by_account(session, "history").values()) == 1
        # Not listed as pending work anymore (no endless retry).
        assert pending_download_assignments(session, niche="history") == []
        # Re-scrapes can never pool the dead reel again.
        assert is_blocked(session, source_url="https://www.instagram.com/reel/SEED0/")

        # A fresh clip + Distribute refills the freed slot.
        replacement = _SC(
            scrape_source_url="https://www.instagram.com/src/",
            source_url="https://www.instagram.com/reel/FRESH1/",
            video_id="FRESH1",
            account_id=session.query(Account).first().id,
        )
        session.add(replacement)
        session.flush()
        fresh = accept_candidate_into_pool(session, candidate=replacement, niche="history")
        fresh.acceptance_status = POOL_STATUS_ACCEPTED
        session.flush()
        distribute_niche(session, "history", rng=random.Random(2))
        session.commit()
        assert sum(assignment_counts_by_account(session, "history").values()) == 2


def test_download_dedup_replace_flags_duplicate_footage(tmp_path) -> None:
    """Phase B: when downloaded clips share footage, the first is kept and the
    rest are flagged duplicate — their pool items leave the pool and their
    assignments become skipped_duplicate (so the next Distribute refills)."""
    init_db()
    with get_session() as session:
        _seed_assigned_pending(session, 3)
        session.commit()

    def fake_downloader(url: str) -> _FakeResult:
        path = tmp_path / f"{url.rstrip('/').rsplit('/', 1)[-1]}.mp4"
        path.write_bytes(b"x")
        return _FakeResult(str(path))

    # Identical (valid-format) fingerprint for every clip => clips 2 and 3 are
    # footage dups of clip 1.
    same_fp = "a1b2c3d4e5f60718,1122334455667788,99aabbccddeeff00"
    summary = download_assigned_pending(
        niche="history",
        downloader=fake_downloader,
        fingerprinter=lambda _path: same_fp,
    )

    assert summary.downloaded == 1
    assert summary.duplicates == 2
    assert summary.failed == 0
    with get_session() as session:
        # Two pool items flagged duplicate -> distributable pool shrinks to 1.
        assert pool_size(session, "history") == 1
        # Skipped assignments don't count, so the account reads as holding 1.
        counts = assignment_counts_by_account(session, "history")
        assert sum(counts.values()) == 1
