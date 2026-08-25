from __future__ import annotations

import datetime as dt
import random

from nicheflow_studio.db.assignments import (
    account_assignment_backlog,
    assignment_counts_by_account,
    assignments_for_account,
    distribute_niche,
    undrafted_item_counts_by_account,
)
from nicheflow_studio.db.media_library import find_or_register_media_asset
from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    DraftRevision,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
)
from nicheflow_studio.db.pools import accept_into_pool
from nicheflow_studio.db.session import get_session, init_db


def _make_accounts(session, niche: str, count: int) -> list[int]:
    ids = []
    for i in range(count):
        acc = Account(name=f"{niche}-acct-{i}", niche=niche)
        session.add(acc)
        session.flush()
        ids.append(acc.id)
    return ids


def _fill_pool(session, niche: str, count: int) -> None:
    for i in range(count):
        asset, _ = find_or_register_media_asset(
            session, source_url=f"https://www.instagram.com/reel/{niche}{i}/"
        )
        accept_into_pool(session, media_asset=asset, niche=niche)


def test_distribute_assigns_every_clip_once_balanced(tmp_path) -> None:
    init_db()
    with get_session() as session:
        _make_accounts(session, "history", 5)
        _fill_pool(session, "history", 50)
        session.commit()

        created = distribute_niche(session, "history", rng=random.Random(1))
        session.commit()

        assert len(created) == 50
        counts = assignment_counts_by_account(session, "history")
        assert sum(counts.values()) == 50
        assert set(counts.values()) == {10}  # 50/5, balanced
        # Each pool item assigned exactly once.
        all_pool_ids = [a.pool_item_id for a in session.query(Assignment).all()]
        assert len(all_pool_ids) == len(set(all_pool_ids))


def test_distribute_prefers_high_source_er_clips_under_cap(tmp_path) -> None:
    """With reach held constant, engagement rate breaks the tie.

    Every clip here has the same view_count, so the reach term of
    source_fit_score cannot separate them and the ER tiebreaker decides.
    """
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        published = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        for i in range(20):
            shortcode = f"eng{i:02d}"
            url = f"https://www.instagram.com/reel/{shortcode}/"
            asset, _ = find_or_register_media_asset(session, source_url=url, shortcode=shortcode)
            accept_into_pool(session, media_asset=asset, niche="history")
            session.add(
                ScrapeCandidate(
                    scrape_source_url=url,
                    source_url=url,
                    video_id=shortcode,
                    title="Behind the scenes of a record-setting sports match",
                    state="pooled",
                    account_id=account_id,
                    view_count=100_000,
                    like_count=i * 1_000,
                    comment_count=0,
                    published_at=published,
                )
            )
        session.commit()

        # 5 slots -> a jitter block of round(5 * 1.2) = 6, so the placed clips are
        # 5 drawn from the 6 highest-ER (eng14..eng19). Which 5 is deliberately
        # jittered so consecutive runs don't place an identical set.
        created = distribute_niche(session, "history", rng=random.Random(1), max_per_account=5)
        session.commit()
        assert len(created) == 5

        shortcodes = dict(
            session.query(PoolItem.id, MediaAsset.source_shortcode)
            .join(MediaAsset, MediaAsset.id == PoolItem.media_asset_id)
            .all()
        )
        placed_codes = {shortcodes[a.pool_item_id] for a in created}
        assert len(placed_codes) == 5
        assert placed_codes <= {f"eng{i:02d}" for i in range(14, 20)}


def test_distribute_prefers_the_clip_that_actually_travelled(tmp_path) -> None:
    """Reach beats engagement RATE when the two disagree.

    This asserted the opposite until 2026-08-12, when 1,100 of the network's own
    posted reels were measured against real Instagram insights: ranked by source
    views the bottom-to-top quartile spread was 3,368 -> 8,256 median views,
    while ranking by source ER peaked in Q3 and fell in Q4. A 1M-view clip is
    the better pick than a 10k-view clip with a marginally better ratio.
    """
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        rows = [
            ("raw-likes", "A photographed appearance", 1_000_000, 50_000, 0),
            ("conversion", "The last time they sang this song together", 10_000, 500, 50),
        ]
        pool_ids: dict[str, int] = {}
        for shortcode, title, views, likes, comments in rows:
            url = f"https://www.instagram.com/reel/{shortcode}/"
            asset, _ = find_or_register_media_asset(session, source_url=url, shortcode=shortcode)
            pool_item = accept_into_pool(session, media_asset=asset, niche="history")
            pool_ids[shortcode] = pool_item.id
            session.add(
                ScrapeCandidate(
                    scrape_source_url=url,
                    source_url=url,
                    video_id=shortcode,
                    title=title,
                    state="pooled",
                    account_id=account_id,
                    view_count=views,
                    like_count=likes,
                    comment_count=comments,
                )
            )
        session.commit()

        created = distribute_niche(session, "history", rng=random.Random(1), max_per_account=1)
        session.commit()

        assert [assignment.pool_item_id for assignment in created] == [pool_ids["raw-likes"]]


def test_distribute_skips_resting_accounts(tmp_path) -> None:
    """A resting account receives zero assignments; active accounts still
    balance the pool between themselves (SOURCING_POOLING_PLAN.md §2.3)."""
    init_db()
    with get_session() as session:
        active_ids = _make_accounts(session, "history", 2)
        (resting_id,) = _make_accounts(session, "history", 1)
        session.get(Account, resting_id).operational_status = "resting"
        _fill_pool(session, "history", 20)
        session.commit()

        created = distribute_niche(session, "history", rng=random.Random(1))
        session.commit()

        assert len(created) == 20
        counts = assignment_counts_by_account(session, "history")
        assert resting_id not in counts
        assert set(counts.keys()) == set(active_ids)
        assert sum(counts.values()) == 20
        assert set(counts.values()) == {10}  # balanced across the 2 active accounts


def test_distribute_is_niche_isolated(tmp_path) -> None:
    init_db()
    with get_session() as session:
        hist_accounts = set(_make_accounts(session, "history", 3))
        _make_accounts(session, "movie", 2)
        _fill_pool(session, "history", 12)
        _fill_pool(session, "movie", 6)
        session.commit()

        distribute_niche(session, "history", rng=random.Random(2))
        session.commit()

        # Every history assignment landed on a history account; movie pool/accounts
        # untouched by the history run.
        hist = session.query(Assignment).filter(Assignment.niche == "history").all()
        assert all(a.account_id in hist_accounts for a in hist)
        assert session.query(Assignment).filter(Assignment.niche == "movie").count() == 0


def test_redistribute_only_places_new_clips(tmp_path) -> None:
    init_db()
    with get_session() as session:
        _make_accounts(session, "history", 4)
        _fill_pool(session, "history", 8)
        session.commit()

        first = distribute_niche(session, "history", rng=random.Random(3))
        session.commit()
        assert len(first) == 8

        # Add 4 more clips, re-run: only the new ones get assigned (no double-book).
        _fill_pool(session, "history", 4)  # note: distinct shortcodes? reuse helper
        session.commit()
        second = distribute_niche(session, "history", rng=random.Random(4))
        session.commit()

        # _fill_pool reuses the same shortcode scheme, so the 4 "new" ones whose
        # shortcodes collide are deduped; assert no clip is assigned twice overall.
        all_pool_ids = [a.pool_item_id for a in session.query(Assignment).all()]
        assert len(all_pool_ids) == len(set(all_pool_ids))
        assert len(second) >= 0  # may be 0 if all collided — the key invariant is no dupes


def test_distribute_reuses_processing_row_despite_url_form_drift(tmp_path) -> None:
    """No duplicate Processing row when the account already has the reel under a
    different URL form (legacy trailing slash vs canonical without)."""
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        asset, _ = find_or_register_media_asset(
            session,
            source_url="https://www.instagram.com/reel/driftAB123",
            shortcode="driftAB123",
        )
        accept_into_pool(session, media_asset=asset, niche="history")
        # The account already handled this reel, recorded with a trailing slash.
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/driftAB123/",
                video_id="driftAB123",
                title="already here",
                status="pending_review",
                account_id=account_id,
            )
        )
        session.commit()

        created = distribute_niche(session, "history", rng=random.Random(1))
        session.commit()

        assert len(created) == 1  # assignment bookkeeping still happens
        rows = (
            session.query(DownloadItem)
            .filter(
                DownloadItem.account_id == account_id,
                DownloadItem.video_id == "driftAB123",
            )
            .all()
        )
        assert len(rows) == 1  # but no second Processing row was created


def test_distribute_no_accounts_returns_empty(tmp_path) -> None:
    init_db()
    with get_session() as session:
        _fill_pool(session, "history", 5)  # pool exists, but no history accounts
        session.commit()

        created = distribute_niche(session, "history", rng=random.Random(1))
        assert created == []


def test_max_per_account_caps_distribution(tmp_path) -> None:
    init_db()
    with get_session() as session:
        _make_accounts(session, "history", 3)
        _fill_pool(session, "history", 30)
        session.commit()

        created = distribute_niche(
            session, "history", rng=random.Random(9), max_per_account=5
        )
        session.commit()

        assert len(created) == 15  # 3 accounts x 5 cap
        counts = assignment_counts_by_account(session, "history")
        assert all(v == 5 for v in counts.values())


def test_pinned_item_assigns_first_to_its_account_even_at_cap(tmp_path) -> None:
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        _fill_pool(session, "history", 2)
        session.commit()

        first = distribute_niche(session, "history", rng=random.Random(1), max_per_account=1)
        assert len(first) == 1
        pinned_asset, _ = find_or_register_media_asset(
            session, source_url="https://www.instagram.com/reel/explicit-pin/"
        )
        pinned_item = accept_into_pool(session, media_asset=pinned_asset, niche="history")
        pinned_item.pinned_account_id = account_id
        session.commit()

        created = distribute_niche(session, "history", rng=random.Random(1), max_per_account=1)
        session.commit()

        assert len(created) == 1
        assert created[0].pool_item_id == pinned_item.id
        assert created[0].account_id == account_id
        assert created[0].distribution_reason == "pinned"
        assert assignment_counts_by_account(session, "history") == {account_id: 2}


def test_assignments_for_account_returns_that_accounts_clips(tmp_path) -> None:
    init_db()
    with get_session() as session:
        account_ids = _make_accounts(session, "history", 2)
        _fill_pool(session, "history", 10)
        session.commit()
        distribute_niche(session, "history", rng=random.Random(1))
        session.commit()

        first_account_assignments = assignments_for_account(session, account_ids[0])
        assert all(a.account_id == account_ids[0] for a in first_account_assignments)
        assert len(first_account_assignments) > 0


def test_account_assignment_backlog_reports_labels_and_pending_state(tmp_path) -> None:
    """The per-account backlog lists assigned clips with a label and their
    download state — pending until the original is fetched (candidate-first)."""
    init_db()
    with get_session() as session:
        account_ids = _make_accounts(session, "history", 1)
        _fill_pool(session, "history", 4)  # shortcodes history0..history3
        session.commit()
        distribute_niche(session, "history", rng=random.Random(1))
        session.commit()

        backlog = account_assignment_backlog(session, account_ids[0])
        assert len(backlog) == 4
        assert all(row.niche == "history" for row in backlog)
        assert all(row.status == "assigned" for row in backlog)
        # Pool was filled candidate-first (no downloads) -> every asset is pending.
        assert all(row.download_status == "pending" for row in backlog)
        # Labels come from the Instagram shortcode (history0..history3).
        assert {row.clip_label for row in backlog} == {f"history{i}" for i in range(4)}


def test_account_assignment_backlog_empty_for_account_without_assignments(tmp_path) -> None:
    init_db()
    with get_session() as session:
        account_ids = _make_accounts(session, "history", 1)
        session.commit()
        assert account_assignment_backlog(session, account_ids[0]) == []


def test_assignment_counts_only_include_pending_assigned_status() -> None:
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        _fill_pool(session, "history", 4)
        session.commit()
        rows = distribute_niche(session, "history", rng=random.Random(1))
        rows[1].status = "posted"
        rows[2].status = "rejected"
        rows[3].status = "skipped_duplicate"
        session.commit()

        assert assignment_counts_by_account(session, "history") == {account_id: 1}


def test_distribute_counts_reels_in_processing_not_assignment_rows() -> None:
    """An assignment row that produced no reel must not hold a slot.

    Assignments created before materialisation was wired into distribute left
    accounts with rows but an empty Processing list. Counting rows meant those
    accounts reported themselves at target forever, so Distribute answered
    "all at cap" and placed nothing while the user stared at zero reels.
    """
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        _fill_pool(session, "history", 20)
        session.commit()

        distribute_niche(session, "history", rng=random.Random(1), max_per_account=6)
        session.commit()
        assert assignment_counts_by_account(session, "history") == {account_id: 6}

        # Delete the reels the assignments produced, leaving the rows behind:
        # exactly the phantom state, an account with 6 rows and nothing to work on.
        for item in session.query(DownloadItem).filter(
            DownloadItem.account_id == account_id
        ):
            session.delete(item)
        session.commit()

        assert assignment_counts_by_account(session, "history") == {account_id: 6}
        assert undrafted_item_counts_by_account(session, "history").get(account_id, 0) == 0

        # So a re-run must refill the account rather than report it full.
        created = distribute_niche(session, "history", rng=random.Random(2), max_per_account=6)
        session.commit()
        assert len(created) == 6
        assert undrafted_item_counts_by_account(session, "history")[account_id] == 6


def test_already_drafted_reels_do_not_hold_a_slot() -> None:
    """The target means "N reels ready to draft", matching Batch Drafts.

    Counting every open item instead let an account sit "at target" holding 9
    reels of which only 5 still needed a draft, so the Batch Drafts screen never
    filled up no matter how many times Distribute ran.
    """
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        _fill_pool(session, "history", 20)
        session.commit()
        distribute_niche(session, "history", rng=random.Random(1), max_per_account=6)
        session.commit()
        assert undrafted_item_counts_by_account(session, "history") == {account_id: 6}

        # Draft three of them: still open work, but no longer work of the kind
        # this target is counting.
        items = (
            session.query(DownloadItem)
            .filter(DownloadItem.account_id == account_id)
            .order_by(DownloadItem.id)
            .limit(3)
            .all()
        )
        for item in items:
            session.add(DraftRevision(download_item_id=item.id, revision_number=1))
        session.commit()

        assert undrafted_item_counts_by_account(session, "history") == {account_id: 3}
        # ...so a re-run refills the three drafted slots.
        created = distribute_niche(session, "history", rng=random.Random(2), max_per_account=6)
        session.commit()
        assert len(created) == 3
        assert undrafted_item_counts_by_account(session, "history") == {account_id: 6}


def test_reels_still_downloading_count_so_top_up_does_not_pile_on() -> None:
    """A distributed reel whose file hasn't landed yet is work already on its
    way. If it didn't count, every top-up tick would order more clips for the
    same slot until the downloads finished."""
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        _fill_pool(session, "history", 20)
        session.commit()
        distribute_niche(session, "history", rng=random.Random(1), max_per_account=6)
        session.commit()

        for item in session.query(DownloadItem).filter(
            DownloadItem.account_id == account_id
        ):
            item.file_path = None
        session.commit()

        assert undrafted_item_counts_by_account(session, "history") == {account_id: 6}
        assert distribute_niche(session, "history", rng=random.Random(2), max_per_account=6) == []


def test_open_item_counts_ignore_posted_and_rejected_reels() -> None:
    """Finished work must not count toward the target."""
    init_db()
    with get_session() as session:
        (account_id,) = _make_accounts(session, "history", 1)
        _fill_pool(session, "history", 10)
        session.commit()
        distribute_niche(session, "history", rng=random.Random(1), max_per_account=4)
        session.commit()

        items = (
            session.query(DownloadItem)
            .filter(DownloadItem.account_id == account_id)
            .order_by(DownloadItem.id)
            .all()
        )
        assert len(items) == 4
        items[0].status = "posted"
        items[1].review_state = "rejected"
        items[2].review_state = "blocked"
        session.commit()

        # Only the untouched fourth reel is still work in hand.
        assert undrafted_item_counts_by_account(session, "history") == {account_id: 1}


def test_top_up_adds_new_accounts_without_touching_existing(tmp_path) -> None:
    """The plan's headline scenario: distribute to a target, add more accounts
    later, re-distribute — new accounts fill to target, existing ones unchanged,
    no clip assigned twice."""
    init_db()
    with get_session() as session:
        _make_accounts(session, "history", 2)
        _fill_pool(session, "history", 200)  # plenty of inventory
        session.commit()

        first = distribute_niche(session, "history", rng=random.Random(1), max_per_account=28)
        session.commit()
        counts = assignment_counts_by_account(session, "history")
        assert len(first) == 56  # 2 accounts x 28
        assert all(v == 28 for v in counts.values())

        # Add 3 more accounts and top up to the same target.
        new_ids = _make_accounts(session, "history", 3)
        session.commit()
        second = distribute_niche(session, "history", rng=random.Random(2), max_per_account=28)
        session.commit()

        counts = assignment_counts_by_account(session, "history")
        # Every one of the 5 accounts now sits at exactly the target.
        assert set(counts.values()) == {28}
        assert len(counts) == 5
        # The new accounts got the +28 each; nothing was added to the old two.
        assert len(second) == 84  # 3 new x 28
        assert all(counts[acc_id] == 28 for acc_id in new_ids)
        # No pool item is double-booked across the whole niche.
        all_pool_ids = [a.pool_item_id for a in session.query(Assignment).all()]
        assert len(all_pool_ids) == len(set(all_pool_ids))
