from __future__ import annotations

import pytest

from nicheflow_studio.core.text_dedup import normalize_caption
from nicheflow_studio.db.media_library import find_or_register_media_asset
from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.pools import (
    CANDIDATE_STATE_POOLED,
    POOL_STATUS_PENDING_REVIEW,
    CrossNicheError,
    DuplicateContentError,
    accept_candidate_into_pool,
    accept_into_pool,
    dedupe_pool_by_caption,
    pool_items_for_niche,
    pool_review_rows,
    pool_size,
    reject_candidate,
    remove_pool_item,
    restore_pool_item,
    set_pool_item_rights_confidence,
)
from nicheflow_studio.db.session import get_session, init_db


def _candidate(session, shortcode: str):
    """A scraped candidate for an Instagram reel, attached to a fresh account."""
    account = Account(name=f"acct-{shortcode}", niche="history")
    session.add(account)
    session.flush()
    candidate = ScrapeCandidate(
        scrape_source_url="https://www.instagram.com/someprofile/",
        source_url=f"https://www.instagram.com/reel/{shortcode}/",
        video_id=shortcode,
        account_id=account.id,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _asset(session, shortcode: str, *, content_hash: str | None = None):
    asset, _ = find_or_register_media_asset(
        session, source_url=f"https://www.instagram.com/reel/{shortcode}/"
    )
    if content_hash is not None:
        asset.content_hash = content_hash
    return asset


# Two fingerprints from the "same footage" (frames match) vs unrelated.
_FP_A = "a1b2c3d4e5f60718,1122334455667788,99aabbccddeeff00"
_FP_A_REPOST = "a1b2c3d4e5f60719,1122334455667789,99aabbccddeeff01"  # 1 bit off each
_FP_B = "0f0f0f0f0f0f0f0f,f0f0f0f0f0f0f0f0,1234123412341234"


def test_duplicate_content_blocked_within_niche(tmp_path) -> None:
    init_db()
    with get_session() as session:
        original = _asset(session, "ORIG01", content_hash=_FP_A)
        accept_into_pool(session, media_asset=original, niche="history")
        session.commit()

        repost = _asset(session, "REPOST1", content_hash=_FP_A_REPOST)
        with pytest.raises(DuplicateContentError) as exc:
            accept_into_pool(session, media_asset=repost, niche="history")
        assert exc.value.existing_asset_id == original.id


def test_duplicate_content_allowed_with_override(tmp_path) -> None:
    init_db()
    with get_session() as session:
        original = _asset(session, "ORIG02", content_hash=_FP_A)
        accept_into_pool(session, media_asset=original, niche="history")
        repost = _asset(session, "REPOST2", content_hash=_FP_A_REPOST)
        item = accept_into_pool(
            session, media_asset=repost, niche="history", allow_duplicate=True
        )
        session.commit()
        assert item.media_asset_id == repost.id


def test_distinct_content_is_not_a_duplicate(tmp_path) -> None:
    init_db()
    with get_session() as session:
        accept_into_pool(session, media_asset=_asset(session, "A1", content_hash=_FP_A), niche="history")
        # Unrelated footage with a different fingerprint accepts fine.
        item = accept_into_pool(
            session, media_asset=_asset(session, "B1", content_hash=_FP_B), niche="history"
        )
        session.commit()
        assert item is not None


def test_no_fingerprint_skips_content_dedup(tmp_path) -> None:
    init_db()
    with get_session() as session:
        accept_into_pool(session, media_asset=_asset(session, "C1", content_hash=_FP_A), niche="history")
        # Asset without a fingerprint can't be content-deduped — must not raise.
        item = accept_into_pool(
            session, media_asset=_asset(session, "C2", content_hash=None), niche="history"
        )
        session.commit()
        assert item is not None


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


def test_pool_contents_reports_source_and_distribution(tmp_path) -> None:
    init_db()
    from nicheflow_studio.db.models import Account, Assignment, PoolItem, ScrapeCandidate
    from nicheflow_studio.db.pools import pool_contents

    with get_session() as session:
        acc = Account(name="Hist A", platform="instagram", niche="history")
        session.add(acc)
        session.flush()
        distributed_asset = _asset(session, "ABC123")
        pending_asset = _asset(session, "XYZ789")
        accept_into_pool(session, media_asset=distributed_asset, niche="history")
        accept_into_pool(session, media_asset=pending_asset, niche="history")
        # Attribute ABC123 to a source owner, and distribute it to the account.
        session.add(
            ScrapeCandidate(
                account_id=acc.id,
                scrape_source_url="https://www.instagram.com/theanomalists/",
                source_url="https://www.instagram.com/reel/ABC123/",
                video_id="ABC123",
                channel_name="theanomalists",
            )
        )
        pi = session.query(PoolItem).filter(PoolItem.media_asset_id == distributed_asset.id).one()
        session.add(Assignment(pool_item_id=pi.id, account_id=acc.id, niche="history"))
        session.commit()

        rows = {r.clip_label: r for r in pool_contents(session, "history")}

    assert rows["ABC123"].source_label == "theanomalists"
    assert rows["ABC123"].is_distributed is True
    assert rows["ABC123"].distributed_to == ("Hist A",)
    assert rows["XYZ789"].is_distributed is False  # ready to distribute
    assert rows["XYZ789"].source_label == "—"  # no candidate => unattributed


# ---------------------------------------------------------------------------
# Candidate-first acceptance (SOURCING_POOLING_PLAN.md §1, §2, §13 Phase 1)
# ---------------------------------------------------------------------------


def test_accept_candidate_creates_pending_asset_and_pool_item(tmp_path) -> None:
    """A candidate enters the pool WITHOUT a download: the backing media asset
    stays 'pending' and the candidate is marked 'pooled'."""
    init_db()
    with get_session() as session:
        candidate = _candidate(session, "CAND01")
        item = accept_candidate_into_pool(session, candidate=candidate, niche="history")
        session.commit()

        assert item.niche == "history"
        assert item.media_asset.download_status == "pending"  # not downloaded yet
        assert candidate.state == CANDIDATE_STATE_POOLED
        # Intake lands in pending_review; the manual review gate accepts it later.
        assert pool_size(session, "history", status=POOL_STATUS_PENDING_REVIEW) == 1


def test_accept_candidate_is_idempotent(tmp_path) -> None:
    """Re-accepting the same candidate returns the existing pool item."""
    init_db()
    with get_session() as session:
        candidate = _candidate(session, "CAND02")
        first = accept_candidate_into_pool(session, candidate=candidate, niche="history")
        second = accept_candidate_into_pool(session, candidate=candidate, niche="history")
        session.commit()

        assert first.id == second.id
        assert pool_size(session, "history", status=POOL_STATUS_PENDING_REVIEW) == 1


def test_two_candidates_same_shortcode_pool_once(tmp_path) -> None:
    """The same reel re-scraped (same shortcode) pools once — URL/shortcode
    dedup before download, not two pool items."""
    init_db()
    with get_session() as session:
        first_candidate = _candidate(session, "DUPE01")
        second_candidate = _candidate(session, "DUPE01")
        item_a = accept_candidate_into_pool(session, candidate=first_candidate, niche="history")
        item_b = accept_candidate_into_pool(session, candidate=second_candidate, niche="history")
        session.commit()

        assert item_a.id == item_b.id
        assert pool_size(session, "history", status=POOL_STATUS_PENDING_REVIEW) == 1


def test_reject_candidate_records_reason_and_creates_no_pool_item(tmp_path) -> None:
    """Rejecting tags the candidate with the specific reason and never pools it
    — so rejected ad/promo clips can never distribute."""
    init_db()
    with get_session() as session:
        candidate = _candidate(session, "ADCLIP1")
        state = reject_candidate(session, candidate=candidate, reason="ad_campaign")
        session.commit()

        assert state == "rejected_ad_campaign"
        assert candidate.state == "rejected_ad_campaign"
        assert pool_size(session, "history") == 0


def test_reject_candidate_rejects_unknown_reason(tmp_path) -> None:
    init_db()
    with get_session() as session:
        candidate = _candidate(session, "X1")
        with pytest.raises(ValueError):
            reject_candidate(session, candidate=candidate, reason="because-i-said-so")


def test_accept_candidate_requires_source_url(tmp_path) -> None:
    init_db()
    with get_session() as session:
        account = Account(name="h", niche="history")
        session.add(account)
        session.flush()
        candidate = ScrapeCandidate(
            scrape_source_url="https://www.instagram.com/p/",
            source_url="",  # no URL to pool
            account_id=account.id,
        )
        session.add(candidate)
        session.flush()
        with pytest.raises(ValueError):
            accept_candidate_into_pool(session, candidate=candidate, niche="history")


def test_niche_pool_stats_counts_pooled_assigned_unused_rejected(tmp_path) -> None:
    """The pool summary reports accepted inventory, how much is assigned, what's
    still unused, and how many candidates were rejected in this niche."""
    init_db()
    import random

    from nicheflow_studio.db.assignments import distribute_niche
    from nicheflow_studio.db.pools import niche_pool_stats

    with get_session() as session:
        account = Account(name="Hist A", platform="instagram", niche="history")
        session.add(account)
        session.flush()
        # 5 accepted clips in the pool.
        for i in range(5):
            accept_into_pool(session, media_asset=_asset(session, f"P{i}"), niche="history")
        # 2 rejected candidates on this history account (never pooled).
        for i in range(2):
            candidate = ScrapeCandidate(
                scrape_source_url="https://www.instagram.com/src/",
                source_url=f"https://www.instagram.com/reel/REJ{i}/",
                video_id=f"REJ{i}",
                account_id=account.id,
            )
            session.add(candidate)
            session.flush()
            reject_candidate(session, candidate=candidate, reason="ad_campaign")
        # Assign 2 of the 5 (single account, cap 2) -> 2 assigned, 3 unused.
        distribute_niche(session, "history", rng=random.Random(1), max_per_account=2)
        session.commit()

        stats = niche_pool_stats(session, "history")
        assert stats.pooled == 5
        assert stats.assigned == 2
        assert stats.unused == 3
        assert stats.rejected == 2
        # Movie niche is empty and isolated.
        assert niche_pool_stats(session, "movie").pooled == 0


# ---------------------------------------------------------------------------
# Pre-download caption dedup (SOURCING_POOLING_PLAN.md §3, dedup-before-download)
# ---------------------------------------------------------------------------


def test_normalize_caption_collapses_emoji_punctuation_and_case() -> None:
    a = normalize_caption("The JFK assassination, explained 🔥🔥")
    b = normalize_caption("the   jfk assassination explained!!!")
    assert a == b == "the jfk assassination explained"
    assert normalize_caption(None) == ""
    assert normalize_caption("   ") == ""


def _pool_clip_with_caption(session, account, shortcode: str, caption: str) -> None:
    asset = _asset(session, shortcode)
    accept_into_pool(session, media_asset=asset, niche="history")
    session.add(
        ScrapeCandidate(
            scrape_source_url="https://www.instagram.com/src/",
            source_url=f"https://www.instagram.com/reel/{shortcode}/",
            video_id=shortcode,
            description=caption,
            account_id=account.id,
        )
    )
    session.flush()


def test_dedupe_pool_by_caption_flags_reposts_keeps_one(tmp_path) -> None:
    """Two clips with the same caption (a cross-source repost) collapse to one;
    the duplicate is flagged so it drops out of the distributable pool."""
    init_db()
    with get_session() as session:
        account = Account(name="Hist", platform="instagram", niche="history")
        session.add(account)
        session.flush()
        # AAA and BBB share a caption after normalization; CCC is unique.
        _pool_clip_with_caption(session, account, "AAA", "The JFK assassination explained 🔥")
        _pool_clip_with_caption(session, account, "BBB", "the jfk assassination explained!!")
        _pool_clip_with_caption(session, account, "CCC", "A totally different fact")
        session.commit()

        assert pool_size(session, "history") == 3
        result = dedupe_pool_by_caption(session, "history")
        session.commit()

        assert result.groups == 1
        assert result.flagged == 1
        # The flagged duplicate is excluded from the accepted/distributable pool.
        assert pool_size(session, "history") == 2
        # Re-running is idempotent (the kept items have no new duplicates).
        again = dedupe_pool_by_caption(session, "history")
        assert again.flagged == 0


def test_dedupe_pool_leaves_uncaptioned_items_untouched(tmp_path) -> None:
    init_db()
    with get_session() as session:
        # Pool items with no originating candidate -> no caption -> not dedup-able.
        accept_into_pool(session, media_asset=_asset(session, "NOCAP1"), niche="history")
        accept_into_pool(session, media_asset=_asset(session, "NOCAP2"), niche="history")
        session.commit()

        result = dedupe_pool_by_caption(session, "history")
        session.commit()
        assert result.flagged == 0
        assert pool_size(session, "history") == 2


# ---------------------------------------------------------------------------
# Manual pool pruning (the post-pool review gate)
# ---------------------------------------------------------------------------


def test_remove_pool_item_drops_it_from_distributable_pool(tmp_path) -> None:
    init_db()
    with get_session() as session:
        accept_into_pool(session, media_asset=_asset(session, "KEEP1"), niche="history")
        bad = accept_into_pool(session, media_asset=_asset(session, "BAD1"), niche="history")
        session.commit()
        assert pool_size(session, "history") == 2

        assert remove_pool_item(session, pool_item_id=bad.id, reason="ad") is True
        session.commit()

        # Removed item no longer counts toward the distributable pool.
        assert pool_size(session, "history") == 1
        remaining = {p.id for p in pool_items_for_niche(session, "history")}
        assert bad.id not in remaining


def test_restore_pool_item_returns_it_to_the_pool(tmp_path) -> None:
    init_db()
    with get_session() as session:
        item = accept_into_pool(session, media_asset=_asset(session, "OOPS1"), niche="history")
        session.commit()
        remove_pool_item(session, pool_item_id=item.id, reason="mistake")
        session.commit()
        assert pool_size(session, "history") == 0

        assert restore_pool_item(session, pool_item_id=item.id) is True
        session.commit()
        assert pool_size(session, "history") == 1


def test_remove_missing_pool_item_returns_false(tmp_path) -> None:
    init_db()
    with get_session() as session:
        assert remove_pool_item(session, pool_item_id=99999) is False


# ---------------------------------------------------------------------------
# Rights-confidence editing (review-time reclassification, SOURCING_POOLING_
# PLAN.md §2.2 rights risk)
# ---------------------------------------------------------------------------


def test_set_pool_item_rights_confidence_persists(tmp_path) -> None:
    init_db()
    with get_session() as session:
        item = accept_into_pool(session, media_asset=_asset(session, "RIGHTS1"), niche="history")
        session.commit()
        item_id = item.id

        updated = set_pool_item_rights_confidence(
            session, pool_item_id=item_id, rights_confidence="broadcast_sport"
        )
        session.commit()
        assert updated is not None
        assert updated.rights_confidence == "broadcast_sport"

    with get_session() as session:
        from nicheflow_studio.db.models import PoolItem

        reloaded = session.get(PoolItem, item_id)
        assert reloaded.rights_confidence == "broadcast_sport"


def test_set_pool_item_rights_confidence_rejects_invalid_value(tmp_path) -> None:
    init_db()
    with get_session() as session:
        item = accept_into_pool(session, media_asset=_asset(session, "RIGHTS2"), niche="history")
        session.commit()
        with pytest.raises(ValueError):
            set_pool_item_rights_confidence(
                session, pool_item_id=item.id, rights_confidence="not-a-real-value"
            )


def test_set_pool_item_rights_confidence_missing_item_returns_none(tmp_path) -> None:
    init_db()
    with get_session() as session:
        assert (
            set_pool_item_rights_confidence(
                session, pool_item_id=999999, rights_confidence="archival"
            )
            is None
        )


def test_pool_review_rows_lists_active_and_optionally_inactive(tmp_path) -> None:
    init_db()
    with get_session() as session:
        a = accept_into_pool(session, media_asset=_asset(session, "RV1"), niche="history")
        accept_into_pool(session, media_asset=_asset(session, "RV2"), niche="history")
        session.commit()
        remove_pool_item(session, pool_item_id=a.id, reason="low quality")
        session.commit()

        active = pool_review_rows(session, "history")
        assert len(active) == 1  # only the accepted one
        assert all(r.status == "accepted" for r in active)

        everything = pool_review_rows(session, "history", include_inactive=True)
        assert len(everything) == 2
        statuses = {r.status for r in everything}
        assert "removed" in statuses and "accepted" in statuses
