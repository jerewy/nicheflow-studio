from __future__ import annotations

from pathlib import Path

import random
import time
from types import SimpleNamespace

from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.db.assignments import distribute_niche
from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
    Source,
)
from nicheflow_studio.db.pools import accept_candidate_into_pool
from nicheflow_studio.db.session import get_session, init_db


def _seed_pending_assigned(clips: int = 2) -> None:
    """A history account with `clips` candidate-first pooled, approved + distributed
    clips (assets stay pending — nothing downloaded)."""
    with get_session() as session:
        account = Account(
            name="Hist 0", platform="instagram", instagram_profile="h0", niche="history"
        )
        session.add(account)
        session.flush()
        for i in range(clips):
            candidate = ScrapeCandidate(
                scrape_source_url="https://www.instagram.com/src/",
                source_url=f"https://www.instagram.com/reel/PEND{i}/",
                video_id=f"PEND{i}",
                account_id=account.id,
            )
            session.add(candidate)
            session.flush()
            item = accept_candidate_into_pool(session, candidate=candidate, niche="history")
            # Candidate-first accepts now land in the 'pending_review' approval
            # gate; approve so the clip is distributable (review -> approve ->
            # distribute), otherwise distribute_niche skips it and nothing pends.
            item.acceptance_status = "accepted"
        session.flush()
        distribute_niche(session, "history", rng=random.Random(1))
        session.commit()


def _seed(history_accounts: int = 2, clips: int = 6) -> None:
    with get_session() as session:
        account_ids: list[int] = []
        for i in range(history_accounts):
            account = Account(
                name=f"Hist {i}",
                platform="instagram",
                instagram_profile=f"h{i}",
                niche="history",
            )
            session.add(account)
            session.flush()
            account_ids.append(account.id)
        for i in range(clips):
            session.add(
                DownloadItem(
                    source_url=f"https://www.instagram.com/reel/SC{i}/",
                    status="downloaded",
                    file_path="x.mp4",
                    video_id=f"SC{i}",
                    account_id=account_ids[i % len(account_ids)] if account_ids else None,
                )
            )
        session.commit()


def _teardown(window: MainWindow) -> None:
    window._refresh_timer.stop()
    window._toast_timer.stop()
    window._hide_toast()
    window.close()


def test_pooling_page_accept_then_distribute(qt_app, tmp_path: Path) -> None:
    init_db()
    _seed(history_accounts=2, clips=6)

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()
        assert window._current_page == "session_health"
        assert window._publishing_dashboard_tabs.currentIndex() == 0

        window._on_pool_accept_clicked("history")
        qt_app.processEvents()
        with get_session() as session:
            assert session.query(PoolItem).filter(PoolItem.niche == "history").count() == 6

        window._on_pool_distribute_clicked("history")
        qt_app.processEvents()
        with get_session() as session:
            assigns = session.query(Assignment).filter(Assignment.niche == "history").all()
            assert len(assigns) == 6
            # Balanced 3/3 across the two accounts; each clip assigned once.
            per_account: dict[int, int] = {}
            for a in assigns:
                per_account[a.account_id] = per_account.get(a.account_id, 0) + 1
            assert sorted(per_account.values()) == [3, 3]
            assert len({a.pool_item_id for a in assigns}) == 6

        # The table reflects the per-account assigned counts.
        rows = window._pooling_table.rowCount()
        counts = {
            window._pooling_table.item(r, 0).text(): window._pooling_table.item(r, 2).text()
            for r in range(rows)
        }
        assert counts["Hist 0"] == "3"
        assert counts["Hist 1"] == "3"
    finally:
        _teardown(window)


def test_pooling_table_shows_target_and_need_and_pool_stats(qt_app, tmp_path: Path) -> None:
    init_db()
    _seed(history_accounts=2, clips=6)

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()
        window._on_pool_accept_clicked("history")
        window._on_pool_distribute_clicked("history")
        qt_app.processEvents()

        # Accounts table now carries Assigned / Target / Need columns.
        assert window._pooling_table.columnCount() == 5
        rows = window._pooling_table.rowCount()
        by_name = {
            window._pooling_table.item(r, 0).text(): (
                window._pooling_table.item(r, 2).text(),  # assigned
                window._pooling_table.item(r, 3).text(),  # target
                window._pooling_table.item(r, 4).text(),  # need
            )
            for r in range(rows)
        }
        assigned, target, need = by_name["Hist 0"]
        assert assigned == "3"
        assert target == "28"  # 4/day x 7-day window
        assert need == "25"  # 28 - 3

        # The niche label reports the pool breakdown.
        label = window._pooling_niche_labels["history"].text()
        assert "6 accepted" in label
        assert "6 assigned" in label
        assert "0 unused" in label
    finally:
        _teardown(window)


def test_pooling_account_backlog_fills_on_selection(qt_app, tmp_path: Path) -> None:
    init_db()
    _seed(history_accounts=2, clips=6)

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()
        window._on_pool_accept_clicked("history")
        window._on_pool_distribute_clicked("history")
        qt_app.processEvents()

        # Nothing selected yet -> backlog is empty with the prompt label.
        assert window._pooling_backlog_table.rowCount() == 0
        assert "Select an account" in window._pooling_backlog_label.text()

        # Selecting an account row fills its backlog (3 clips each). This seed
        # uses the download-first accept path, so assets are already downloaded
        # (the candidate-first 'pending' case is covered in test_assignments).
        window._pooling_table.selectRow(0)
        qt_app.processEvents()
        assert window._pooling_backlog_table.rowCount() == 3
        downloads = {
            window._pooling_backlog_table.item(r, 2).text()
            for r in range(window._pooling_backlog_table.rowCount())
        }
        assert downloads == {"downloaded"}
        assert "3 assigned clip(s)" in window._pooling_backlog_label.text()
    finally:
        _teardown(window)


def test_pool_download_assigned_no_pending_shows_info(qt_app, tmp_path: Path) -> None:
    init_db()
    _seed(history_accounts=1, clips=0)  # an account, but nothing assigned

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()
        window._on_pool_download_assigned_clicked("history")
        qt_app.processEvents()
        assert getattr(window, "_pool_download_in_progress", False) is False
        assert "No assigned history clips" in window._status_label.text()
    finally:
        _teardown(window)


def test_pool_download_assigned_runs_and_reports(qt_app, tmp_path: Path, monkeypatch) -> None:
    init_db()
    _seed_pending_assigned(clips=2)

    # Avoid the network: the worker thread calls main_window.download_assigned_pending.
    from nicheflow_studio.app import main_window as mw

    monkeypatch.setattr(
        mw,
        "download_assigned_pending",
        lambda **_kwargs: SimpleNamespace(
            downloaded=2, reused=0, failed=0, duplicates=0, errors=()
        ),
    )

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()
        window._on_pool_download_assigned_clicked("history")

        deadline = time.time() + 5.0
        while getattr(window, "_pool_download_in_progress", False) and time.time() < deadline:
            qt_app.processEvents()
            time.sleep(0.02)

        assert getattr(window, "_pool_download_in_progress", False) is False
        assert all(b.isEnabled() for b in window._pooling_download_buttons.values())
        assert "2 downloaded" in window._status_label.text()
    finally:
        _teardown(window)


def test_pooling_distribute_without_accounts_is_safe(qt_app, tmp_path: Path) -> None:
    init_db()
    # Clips exist but no movie accounts.
    _seed(history_accounts=0, clips=4)

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()
        assert window._current_page == "session_health"
        assert window._publishing_dashboard_tabs.tabText(0) == "1. Pool & Distribute"

        window._on_pool_accept_clicked("movie")
        window._on_pool_distribute_clicked("movie")  # no movie accounts -> no-op, no crash
        qt_app.processEvents()

        with get_session() as session:
            assert session.query(Assignment).count() == 0
    finally:
        _teardown(window)


def test_pooling_actions_do_not_overlap_account_table(qt_app, tmp_path: Path) -> None:
    init_db()
    _seed(history_accounts=6, clips=8)

    window = MainWindow()
    try:
        window.resize(1400, 820)
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()

        table_bottom = window._pooling_table.geometry().bottom()
        actions_top = window._pooling_prep_actions.geometry().top()
        # The table grows to fit its rows (6 seeded accounts -> more than the
        # single row it used to clip to) but stays capped so a large network
        # scrolls instead of pushing the action buttons off-screen.
        header_height = window._pooling_table.horizontalHeader().height()
        assert window._pooling_table.height() > header_height + 100
        assert window._pooling_table.maximumHeight() <= 360
        assert actions_top > table_bottom
    finally:
        _teardown(window)


def test_pool_niche_filter_limits_table_to_selected_niche(qt_app, tmp_path: Path) -> None:
    init_db()
    with get_session() as session:
        session.add(Account(name="Hist A", platform="instagram", instagram_profile="ha", niche="history"))
        session.add(Account(name="Movie A", platform="instagram", instagram_profile="ma", niche="movie"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()

        # Select History -> only history accounts appear.
        idx = window._pooling_niche_combo.findData("history")
        window._pooling_niche_combo.setCurrentIndex(idx)
        qt_app.processEvents()
        names = {
            window._pooling_table.item(r, 0).text()
            for r in range(window._pooling_table.rowCount())
        }
        assert "Hist A" in names and "Movie A" not in names
    finally:
        _teardown(window)


def test_pool_add_source_registers_one_niche_seed_source(qt_app, tmp_path: Path) -> None:
    init_db()
    with get_session() as session:
        session.add(Account(name="Hist A", platform="instagram", instagram_profile="ha", niche="history"))
        session.add(Account(name="Hist B", platform="instagram", instagram_profile="hb", niche="history"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()

        window._pooling_niche_combo.setCurrentIndex(window._pooling_niche_combo.findData("history"))
        window._pooling_source_input.setText("https://www.instagram.com/thehistologian/")
        window._on_pool_add_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            sources = session.query(Source).all()
            urls = [s.source_url for s in sources]
        assert len(sources) == 1
        assert any("thehistologian" in u for u in urls)
    finally:
        _teardown(window)


def test_pool_accept_only_uses_downloads_from_that_niche(qt_app, tmp_path: Path) -> None:
    init_db()
    with get_session() as session:
        history = Account(name="Hist A", platform="instagram", instagram_profile="ha", niche="history")
        movie = Account(name="Movie A", platform="instagram", instagram_profile="ma", niche="movie")
        session.add_all([history, movie])
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/HIST01/",
                status="downloaded",
                file_path="hist.mp4",
                video_id="HIST01",
                account_id=history.id,
            )
        )
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/MOV01/",
                status="downloaded",
                file_path="mov.mp4",
                video_id="MOV01",
                account_id=movie.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()

        window._on_pool_accept_clicked("history")
        qt_app.processEvents()

        with get_session() as session:
            history_items = session.query(PoolItem).filter(PoolItem.niche == "history").all()
            assert len(history_items) == 1
            assert history_items[0].media_asset.source_shortcode == "HIST01"
            assert session.query(PoolItem).filter(PoolItem.niche == "movie").count() == 0
    finally:
        _teardown(window)


def test_pool_source_inventory_shows_scraped_source_counts(qt_app, tmp_path: Path) -> None:
    init_db()

    with get_session() as session:
        account = Account(
            name="Past Moments Daily",
            platform="instagram",
            instagram_profile="pastmomentsdaily",
            niche="history",
        )
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@theanomalists",
            source_url="https://www.instagram.com/theanomalists/",
        )
        session.add(source)
        session.flush()
        item = DownloadItem(
            source_url="https://www.instagram.com/reel/ANOM01/",
            status="downloaded",
            file_path="anom.mp4",
            video_id="ANOM01",
            account_id=account.id,
        )
        session.add(item)
        session.flush()
        session.add(
            ScrapeCandidate(
                scrape_source_url=source.source_url,
                source_url=item.source_url,
                extractor="instagram",
                video_id=item.video_id,
                title="Anomalists clip",
                channel_name="theanomalists",
                state="downloaded",
                queued_download_item_id=item.id,
                source_id=source.id,
                account_id=account.id,
            )
        )
        session.add(
            ScrapeCandidate(
                scrape_source_url=source.source_url,
                source_url="https://www.instagram.com/reel/ANOM02/",
                extractor="instagram",
                video_id="ANOM02",
                title="Second clip",
                channel_name="theanomalists",
                state="candidate",
                source_id=source.id,
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        window._pooling_niche_combo.setCurrentIndex(window._pooling_niche_combo.findData("history"))
        qt_app.processEvents()

        assert window._pooling_source_table.rowCount() == 1
        assert window._pooling_source_table.item(0, 0).text() == "Combined sources"
        assert window._pooling_source_table.horizontalHeaderItem(1).text() == "Niche"
        assert window._pooling_source_table.item(0, 1).text() == "History"
        assert window._pooling_source_table.item(0, 2).text() == "1"
        assert window._pooling_source_table.item(0, 3).text() == "2"
        assert window._pooling_source_table.item(0, 4).text() == "2"
    finally:
        _teardown(window)


def test_pool_source_inventory_groups_duplicate_seed_sources(qt_app, tmp_path: Path) -> None:
    init_db()

    with get_session() as session:
        first = Account(
            name="Past Moments Daily",
            platform="instagram",
            instagram_profile="pastmomentsdaily",
            niche="history",
        )
        second = Account(
            name="History Clips Daily",
            platform="instagram",
            instagram_profile="historyclipsdaily",
            niche="history",
        )
        session.add_all([first, second])
        session.flush()
        first_source = Source(
            account_id=first.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@crazyfactscorner",
            source_url="https://www.instagram.com/crazyfactscorner/",
        )
        second_source = Source(
            account_id=second.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@crazyfactscorner",
            source_url="https://www.instagram.com/crazyfactscorner/",
        )
        session.add_all([first_source, second_source])
        session.flush()
        session.add(
            ScrapeCandidate(
                scrape_source_url=first_source.source_url,
                source_url="https://www.instagram.com/reel/CRAZY01/",
                extractor="instagram",
                video_id="CRAZY01",
                title="Crazy facts clip",
                channel_name="crazyfactscorner",
                state="candidate",
                source_id=first_source.id,
                account_id=first.id,
            )
        )
        session.add(
            ScrapeCandidate(
                scrape_source_url=second_source.source_url,
                source_url="https://www.instagram.com/reel/CRAZY01/?utm_source=copy",
                extractor="instagram",
                video_id="CRAZY01",
                title="Same crazy facts clip",
                channel_name="crazyfactscorner",
                state="candidate",
                source_id=second_source.id,
                account_id=second.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        window._pooling_niche_combo.setCurrentIndex(window._pooling_niche_combo.findData("history"))
        qt_app.processEvents()

        assert window._pooling_source_table.rowCount() == 1
        assert window._pooling_source_table.item(0, 0).text() == "Combined sources"
        assert window._pooling_source_table.item(0, 1).text() == "History"
        assert window._pooling_source_table.item(0, 2).text() == "1"
        assert window._pooling_source_table.item(0, 3).text() == "2"
        assert window._pooling_source_table.item(0, 4).text() == "1"
        assert window._pooling_source_table.item(0, 5).text() == "1"
    finally:
        _teardown(window)


def test_pool_source_summary_counts_unique_candidates_across_sources(
    qt_app, tmp_path: Path
) -> None:
    init_db()

    with get_session() as session:
        account = Account(
            name="Past Moments Daily",
            platform="instagram",
            instagram_profile="pastmomentsdaily",
            niche="history",
        )
        session.add(account)
        session.flush()
        anomalists = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@theanomalists",
            source_url="https://www.instagram.com/theanomalists/",
        )
        crazy = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@crazyfactscorner",
            source_url="https://www.instagram.com/crazyfactscorner/",
        )
        session.add_all([anomalists, crazy])
        session.flush()
        session.add(
            ScrapeCandidate(
                scrape_source_url=anomalists.source_url,
                source_url="https://www.instagram.com/reel/SAME01/",
                extractor="instagram",
                video_id="SAME01",
                title="Shared clip",
                channel_name="theanomalists",
                state="candidate",
                source_id=anomalists.id,
                account_id=account.id,
            )
        )
        session.add(
            ScrapeCandidate(
                scrape_source_url=crazy.source_url,
                source_url="https://www.instagram.com/reel/SAME01/?utm_source=copy",
                extractor="instagram",
                video_id="SAME01",
                title="Shared clip repost",
                channel_name="crazyfactscorner",
                state="candidate",
                source_id=crazy.id,
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        window._pooling_niche_combo.setCurrentIndex(window._pooling_niche_combo.findData("history"))
        qt_app.processEvents()

        assert "2 scraped, 1 unique, 1 duplicate across sources" in (
            window._pooling_sources_label.text()
        )
    finally:
        _teardown(window)


def test_pool_source_inventory_counts_candidates(qt_app) -> None:
    # Footage-fingerprint dedup used to be computed here (an O(n^2) match over the
    # whole pool on every page refresh), which hung the page on a large pool. It's
    # been retired from the page — footage dedup runs in the dedicated download/
    # fingerprint flow instead — so the (now hidden) "video dupes" column reads 0.
    init_db()

    with get_session() as session:
        account = Account(
            name="Past Moments Daily",
            platform="instagram",
            instagram_profile="pastmomentsdaily",
            niche="history",
        )
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@theanomalists",
            source_url="https://www.instagram.com/theanomalists/",
        )
        session.add(source)
        session.flush()
        first_asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://www.instagram.com/reel/VIDEOA",
            source_shortcode="VIDEOA",
            download_status="downloaded",
            content_hash="1111111111111111,2222222222222222",
        )
        second_asset = MediaAsset(
            platform="instagram",
            canonical_source_url="https://www.instagram.com/reel/VIDEOB",
            source_shortcode="VIDEOB",
            download_status="downloaded",
            content_hash="1111111111111111,2222222222222222",
        )
        session.add_all([first_asset, second_asset])
        session.add_all(
            [
                ScrapeCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.instagram.com/reel/VIDEOA/",
                    extractor="instagram",
                    video_id="VIDEOA",
                    title="First video",
                    channel_name="theanomalists",
                    state="candidate",
                    source_id=source.id,
                    account_id=account.id,
                ),
                ScrapeCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.instagram.com/reel/VIDEOB/",
                    extractor="instagram",
                    video_id="VIDEOB",
                    title="Second repost",
                    channel_name="theanomalists",
                    state="candidate",
                    source_id=source.id,
                    account_id=account.id,
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        window._pooling_niche_combo.setCurrentIndex(window._pooling_niche_combo.findData("history"))
        qt_app.processEvents()

        assert window._pooling_source_table.item(0, 3).text() == "2"  # scraped
        assert window._pooling_source_table.item(0, 4).text() == "2"  # unique URLs
        assert window._pooling_source_table.item(0, 5).text() == "0"  # URL dupes
        # Video-dupe counting retired from the page refresh (was an O(n^2) hang).
        assert window._pooling_source_table.item(0, 6).text() == "0"
    finally:
        _teardown(window)


def test_pool_archive_scrape_starts_selected_source_job(qt_app, monkeypatch) -> None:
    init_db()
    captured = {}

    with get_session() as session:
        account = Account(
            name="Past Moments Daily",
            platform="instagram",
            instagram_profile="pastmomentsdaily",
            niche="history",
            scrape_max_items=50,
        )
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@crazyfactscorner",
            source_url="https://www.instagram.com/crazyfactscorner/",
        )
        session.add(source)
        session.commit()
        source_id = source.id

    window = MainWindow()
    try:
        monkeypatch.setattr(window, "_confirm_instagram_scrape", lambda **_: True)

        def fake_start_scrape_job(job):
            captured["account_id"] = job.account_id
            captured["source_ids"] = job.source_ids
            captured["max_items"] = job.max_items
            captured["max_age_days"] = job.max_age_days
            captured["min_view_count"] = job.min_view_count
            captured["min_like_count"] = job.min_like_count
            captured["archive_backfill"] = job.archive_backfill

        monkeypatch.setattr(window, "_start_scrape_job", fake_start_scrape_job)

        window.show()
        window._set_current_page("pooling")
        window._pooling_niche_combo.setCurrentIndex(window._pooling_niche_combo.findData("history"))
        window._pooling_archive_limit_input.setValue(1314)
        qt_app.processEvents()

        window._on_pool_scrape_source_clicked(archive_backfill=True)

        assert captured["source_ids"] == [source_id]
        assert captured["max_items"] == 1314
        assert captured["max_age_days"] is None
        assert captured["min_view_count"] == 0
        assert captured["min_like_count"] == 0
        assert captured["archive_backfill"] is True
    finally:
        _teardown(window)
