from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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

        window._on_account_health_result(
            {
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
        assert table.item(respawn_row, 3).text() == "Confirmed logged in as @respawn"
    finally:
        _teardown(window)
