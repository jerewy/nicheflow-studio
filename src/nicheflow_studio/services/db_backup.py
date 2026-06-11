"""Automatic SQLite-safe database backups for the webview app."""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from nicheflow_studio.core.paths import backups_dir
from nicheflow_studio.db.session import database_path

logger = logging.getLogger(__name__)

_BACKUP_GLOB = "nicheflow-backup-*.zip"
_BACKUP_INTERVAL = dt.timedelta(hours=24)
_BACKUP_RETENTION = 14


def _backup_paths(backup_root: Path) -> list[Path]:
    return sorted(
        backup_root.glob(_BACKUP_GLOB),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _prune_backups(backup_root: Path, *, keep: int = _BACKUP_RETENTION) -> None:
    for old_backup in _backup_paths(backup_root)[max(0, keep) :]:
        old_backup.unlink()


def _backup_is_due(backup_root: Path, *, now: dt.datetime) -> bool:
    backups = _backup_paths(backup_root)
    if not backups:
        return True
    newest_modified = dt.datetime.fromtimestamp(backups[0].stat().st_mtime, dt.timezone.utc)
    return now - newest_modified >= _BACKUP_INTERVAL


def _write_database_backup(source_path: Path, backup_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="nicheflow-db-backup-", dir=backup_path.parent) as temp:
        snapshot_path = Path(temp) / source_path.name
        with closing(sqlite3.connect(source_path)) as source, closing(
            sqlite3.connect(snapshot_path)
        ) as snapshot:
            source.backup(snapshot)

        temp_zip = backup_path.with_suffix(".zip.tmp")
        try:
            with zipfile.ZipFile(temp_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot_path, arcname=source_path.name)
            temp_zip.replace(backup_path)
        finally:
            temp_zip.unlink(missing_ok=True)


def backup_database_if_due(*, now: dt.datetime | None = None) -> Path | None:
    """Create a safe database backup when the newest backup is at least 24h old."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    backup_root = backups_dir()
    backup_root.mkdir(parents=True, exist_ok=True)
    if not _backup_is_due(backup_root, now=now):
        _prune_backups(backup_root)
        return None

    source_path = database_path()
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    backup_path = backup_root / f"nicheflow-backup-{timestamp}.zip"
    _write_database_backup(source_path, backup_path)
    _prune_backups(backup_root)
    return backup_path


def run_startup_backup() -> Path | None:
    """Best-effort startup backup; failures never block the webview app."""
    try:
        backup_path = backup_database_if_due()
    except Exception:
        logger.warning(
            "Automatic database backup failed; app startup will continue.", exc_info=True
        )
        return None
    if backup_path is not None:
        logger.info("Created automatic database backup: %s", backup_path)
    return backup_path
