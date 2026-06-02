from __future__ import annotations

from pathlib import Path

from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.db.models import Account, Assignment, DownloadItem, PoolItem
from nicheflow_studio.db.session import get_session, init_db


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


def test_pool_add_source_attaches_to_niche_accounts(qt_app, tmp_path: Path) -> None:
    init_db()
    with get_session() as session:
        session.add(Account(name="Hist A", platform="instagram", instagram_profile="ha", niche="history"))
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

        from nicheflow_studio.db.models import Source
        with get_session() as session:
            urls = [s.source_url for s in session.query(Source).all()]
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
    from nicheflow_studio.db.models import ScrapeCandidate, Source

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
        assert window._pooling_source_table.item(0, 0).text() == "@theanomalists"
        assert window._pooling_source_table.item(0, 2).text() == "2"
        assert window._pooling_source_table.item(0, 3).text() == "1"
    finally:
        _teardown(window)
