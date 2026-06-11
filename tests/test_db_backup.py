from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from nicheflow_studio.core.paths import backups_dir
from nicheflow_studio.db.models import Account
from nicheflow_studio.db.session import database_path, get_session, init_db
from nicheflow_studio.services import db_backup


def _utc(year: int, month: int, day: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=dt.timezone.utc)


def test_fresh_start_creates_restorable_database_zip() -> None:
    init_db()
    with get_session() as session:
        session.add(Account(name="Backup Account", platform="instagram"))
        session.commit()

    backup_path = db_backup.backup_database_if_due(now=_utc(2026, 6, 11, 12))

    assert backup_path is not None
    assert backup_path.parent == backups_dir()
    assert backup_path.name == "nicheflow-backup-20260611-120000.zip"
    with zipfile.ZipFile(backup_path) as archive:
        assert archive.namelist() == ["nicheflow.db"]
        archive.extract("nicheflow.db", path=backup_path.parent / "restore-check")

    restored = backup_path.parent / "restore-check" / "nicheflow.db"
    with sqlite3.connect(restored) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM accounts")]
    assert names == ["Backup Account"]


def test_restart_within_24_hours_does_not_create_second_zip() -> None:
    init_db()
    first = db_backup.backup_database_if_due(now=_utc(2026, 6, 11, 12))
    assert first is not None
    os.utime(first, (_utc(2026, 6, 11, 12).timestamp(),) * 2)

    second = db_backup.backup_database_if_due(now=_utc(2026, 6, 12, 11))

    assert second is None
    assert list(backups_dir().glob("nicheflow-backup-*.zip")) == [first]


def test_startup_prunes_to_14_newest_backups() -> None:
    init_db()
    backup_root = backups_dir()
    backup_root.mkdir(parents=True, exist_ok=True)
    newest_time = _utc(2026, 6, 10, 0).timestamp()
    for index in range(15):
        path = backup_root / f"nicheflow-backup-20260610-{index:06d}.zip"
        path.write_bytes(b"fake")
        modified = newest_time - (14 - index)
        os.utime(path, (modified, modified))

    result = db_backup.backup_database_if_due(now=_utc(2026, 6, 10, 0))

    assert result is None
    remaining = sorted(backup_root.glob("nicheflow-backup-*.zip"))
    assert len(remaining) == 14
    assert not (backup_root / "nicheflow-backup-20260610-000000.zip").exists()


def test_backup_write_failure_warns_and_startup_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    init_db()

    def fail_write(source_path: Path, backup_path: Path) -> None:
        raise PermissionError("read-only backup directory")

    monkeypatch.setattr(db_backup, "_write_database_backup", fail_write)

    with caplog.at_level(logging.WARNING):
        result = db_backup.run_startup_backup()

    assert result is None
    assert database_path().exists()
    assert "app startup will continue" in caplog.text
