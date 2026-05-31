from __future__ import annotations

from pathlib import Path

from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.db.models import Account, Assignment, DownloadItem, PoolItem
from nicheflow_studio.db.session import get_session, init_db


def _seed(history_accounts: int = 2, clips: int = 6) -> None:
    with get_session() as session:
        for i in range(history_accounts):
            session.add(
                Account(
                    name=f"Hist {i}",
                    platform="instagram",
                    instagram_profile=f"h{i}",
                    niche="history",
                )
            )
        for i in range(clips):
            session.add(
                DownloadItem(
                    source_url=f"https://www.instagram.com/reel/SC{i}/",
                    status="downloaded",
                    file_path="x.mp4",
                    video_id=f"SC{i}",
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
        assert window._current_page == "pooling"

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


def test_pooling_distribute_without_accounts_is_safe(qt_app, tmp_path: Path) -> None:
    init_db()
    # Clips exist but no movie accounts.
    _seed(history_accounts=0, clips=4)

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("pooling")
        qt_app.processEvents()

        window._on_pool_accept_clicked("movie")
        window._on_pool_distribute_clicked("movie")  # no movie accounts -> no-op, no crash
        qt_app.processEvents()

        with get_session() as session:
            assert session.query(Assignment).count() == 0
    finally:
        _teardown(window)
