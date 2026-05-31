from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHeaderView

import nicheflow_studio.app.main_window as mw
from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.core.account_health import HealthState, SessionHealth
from nicheflow_studio.db.models import Account
from nicheflow_studio.db.session import get_session, init_db


def _fake_local_health(profile_name, account_name=None, **_):
    return SessionHealth(
        profile_name=profile_name,
        account_name=account_name,
        state=HealthState.NO_SESSION,
        detail="No saved login.",
        checked_at=datetime.now(timezone.utc),
        is_live=False,
    )


def _make_accounts() -> None:
    with get_session() as session:
        session.add_all(
            [
                Account(
                    name="RespawnReels",
                    platform="instagram",
                    instagram_profile="main",
                    login_identifier="me@example.com",
                ),
                Account(name="Alt One", platform="instagram", instagram_profile="alt1"),
            ]
        )
        session.commit()


def _teardown(window: MainWindow) -> None:
    window._refresh_timer.stop()
    window._toast_timer.stop()
    window._hide_toast()
    window.close()


def test_accounts_page_lists_local_session_health(qt_app, monkeypatch, tmp_path: Path) -> None:
    init_db()
    _make_accounts()
    # Stub local health so the row status is independent of real on-disk profiles
    # (instagram_session resolves profile paths at import time, not under tmp).
    monkeypatch.setattr(mw, "local_health", _fake_local_health)
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._open_session_health_dialog()
        qt_app.processEvents()

        table = window._account_health_table
        assert table.rowCount() == 2
        statuses = {table.item(row, 2).text() for row in range(table.rowCount())}
        assert statuses == {"No login"}
        assert "2 account(s)" in window._account_health_summary.text()
    finally:
        _teardown(window)


def test_sidebar_health_button_opens_tab(qt_app, tmp_path: Path) -> None:
    init_db()
    _make_accounts()
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        assert window._current_page != "session_health"
        window._sidebar_health_button.click()
        qt_app.processEvents()
        # Session Health is an in-window tab now, not a floating dialog.
        assert window._current_page == "session_health"
        assert window._account_health_table.rowCount() == 2
    finally:
        _teardown(window)


def test_publishing_dashboard_visible_without_selected_account(qt_app, tmp_path: Path) -> None:
    init_db()
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        assert window._current_account_id is None

        window._sidebar_health_button.click()
        qt_app.processEvents()

        assert window._current_page == "session_health"
        assert window._workspace_content.isVisible() is True
        assert window._library_gate_panel.isVisible() is False
        assert window._publishing_dashboard_tabs.tabText(0) == "1. Pool & Distribute"
        assert window._sidebar_toggle_button.isEnabled() is True

        window._toggle_account_sidebar()
        qt_app.processEvents()

        assert window._account_panel.isVisible() is False
        assert window._workspace_content.isVisible() is True
    finally:
        _teardown(window)


def test_account_readiness_detail_column_is_readable(qt_app, monkeypatch, tmp_path: Path) -> None:
    init_db()
    _make_accounts()
    monkeypatch.setattr(mw, "local_health", _fake_local_health)
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._open_session_health_dialog()
        qt_app.processEvents()

        table = window._account_health_table
        header = table.horizontalHeader()
        assert table.wordWrap() is True
        assert table.textElideMode() == Qt.TextElideMode.ElideNone
        assert header.sectionResizeMode(6) == QHeaderView.ResizeMode.Stretch
        for column in (0, 1, 2, 3, 4, 5):
            assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.ResizeToContents

        detail_item = table.item(0, 6)
        assert detail_item is not None
        assert detail_item.toolTip() == detail_item.text()
    finally:
        _teardown(window)


def test_health_selection_gates_relogin_and_copy_email(qt_app, tmp_path: Path) -> None:
    init_db()
    _make_accounts()
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._open_session_health_dialog()
        qt_app.processEvents()

        table = window._account_health_table
        # Rows are ordered by account name: "Alt One" (no email), then "RespawnReels".
        alt_row = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "Alt One")
        respawn_row = next(
            r for r in range(table.rowCount()) if table.item(r, 0).text() == "RespawnReels"
        )

        table.selectRow(respawn_row)
        qt_app.processEvents()
        assert window._account_relogin_button.isEnabled() is True
        assert window._account_copy_email_button.isEnabled() is True

        table.selectRow(alt_row)
        qt_app.processEvents()
        assert window._account_relogin_button.isEnabled() is True
        assert window._account_copy_email_button.isEnabled() is False  # no email saved
    finally:
        _teardown(window)


def test_live_result_updates_matching_row(qt_app, tmp_path: Path) -> None:
    init_db()
    _make_accounts()
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._open_session_health_dialog()
        qt_app.processEvents()

        # Live results are matched to rows by account_name (multiple accounts can
        # share one profile, so profile_name alone is ambiguous).
        window._on_account_health_result(
            {
                "account_name": "RespawnReels",
                "profile_name": "main",
                "state": HealthState.OK,
                "detail": "Confirmed logged in as @respawn",
            }
        )
        table = window._account_health_table
        respawn_row = next(
            r for r in range(table.rowCount()) if table.item(r, 0).text() == "RespawnReels"
        )
        assert table.item(respawn_row, 2).text() == "OK"
        assert table.item(respawn_row, 6).text() == "Confirmed logged in as @respawn"
    finally:
        _teardown(window)


def test_mismatch_result_renders_wrong_account(qt_app, tmp_path: Path) -> None:
    init_db()
    _make_accounts()
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._open_session_health_dialog()
        qt_app.processEvents()

        window._on_account_health_result(
            {
                "account_name": "RespawnReels",
                "profile_name": "main",
                "state": HealthState.MISMATCH,
                "detail": "Logged in as @other, but this account expects @respawn.",
            }
        )
        table = window._account_health_table
        row = next(
            r for r in range(table.rowCount()) if table.item(r, 0).text() == "RespawnReels"
        )
        assert table.item(row, 2).text() == "Wrong account"
        assert "expects @respawn" in table.item(row, 6).text()
    finally:
        _teardown(window)


def test_blank_profile_is_unconfigured_not_main(qt_app, monkeypatch, tmp_path: Path) -> None:
    """An account with no profile must read as 'No profile', never borrow 'main'."""
    init_db()
    with get_session() as session:
        session.add(
            Account(name="No Profile", platform="instagram", instagram_profile=None)
        )
        session.commit()
    monkeypatch.setattr(mw, "local_health", _fake_local_health)
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._open_session_health_dialog()
        qt_app.processEvents()

        table = window._account_health_table
        row = next(
            r for r in range(table.rowCount()) if table.item(r, 0).text() == "No Profile"
        )
        assert table.item(row, 1).text() == "—"  # profile column shows unset, not "main"
        assert table.item(row, 2).text() == "No profile"
    finally:
        _teardown(window)


def test_shared_profile_is_flagged_as_collision(qt_app, monkeypatch, tmp_path: Path) -> None:
    """Two accounts on one profile share a single Instagram login — warn about it."""
    init_db()
    with get_session() as session:
        session.add_all(
            [
                Account(name="Shared A", platform="instagram", instagram_profile="main"),
                Account(name="Shared B", platform="instagram", instagram_profile="main"),
            ]
        )
        session.commit()
    monkeypatch.setattr(mw, "local_health", _fake_local_health)
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._open_session_health_dialog()
        qt_app.processEvents()

        table = window._account_health_table
        a_row = next(
            r for r in range(table.rowCount()) if table.item(r, 0).text() == "Shared A"
        )
        detail = table.item(a_row, 6).text()
        assert "shares profile 'main'" in detail
        assert "Shared B" in detail
        assert "shared by multiple accounts" in window._account_health_summary.text()
    finally:
        _teardown(window)
