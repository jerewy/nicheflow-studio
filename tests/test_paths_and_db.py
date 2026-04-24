from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import nicheflow_studio.core.paths as path_module
from nicheflow_studio.core.paths import (
    backups_dir,
    data_dir,
    downloads_dir,
    ensure_data_dirs,
    logs_dir,
    processed_dir,
)
from nicheflow_studio.db.session import init_db


def test_default_data_paths_use_repo_local_data_dir() -> None:
    ensure_data_dirs()

    assert data_dir().name == "data"
    assert downloads_dir() == Path.cwd() / "data" / "downloads"
    assert backups_dir() == Path.cwd() / "data" / "backups"
    assert logs_dir() == Path.cwd() / "data" / "logs"
    assert processed_dir() == Path.cwd() / "data" / "processed"
    assert downloads_dir().is_dir()
    assert backups_dir().is_dir()
    assert logs_dir().is_dir()
    assert processed_dir().is_dir()


def test_data_dir_can_be_overridden(monkeypatch) -> None:
    custom_dir = Path.cwd() / "custom-runtime"
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(custom_dir))

    ensure_data_dirs()
    init_db()

    assert data_dir() == custom_dir.resolve()
    assert downloads_dir() == custom_dir.resolve() / "downloads"
    assert backups_dir() == custom_dir.resolve() / "backups"
    assert logs_dir() == custom_dir.resolve() / "logs"
    assert (custom_dir / "nicheflow.db").exists()


def test_packaged_windows_defaults_to_local_appdata(monkeypatch) -> None:
    local_appdata = Path.cwd() / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setattr(path_module.sys, "frozen", True, raising=False)

    ensure_data_dirs()

    expected = local_appdata.resolve() / "NicheFlow Studio" / "data"
    assert data_dir() == expected
    assert downloads_dir() == expected / "downloads"
    assert backups_dir() == expected / "backups"
    assert logs_dir() == expected / "logs"
    assert downloads_dir().is_dir()
    assert backups_dir().is_dir()
    assert logs_dir().is_dir()


def test_override_wins_even_for_packaged_runtime(monkeypatch) -> None:
    local_appdata = Path.cwd() / "LocalAppData"
    custom_dir = Path.cwd() / "custom-runtime"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(custom_dir))
    monkeypatch.setattr(path_module.sys, "frozen", True, raising=False)

    ensure_data_dirs()

    assert data_dir() == custom_dir.resolve()
    assert downloads_dir() == custom_dir.resolve() / "downloads"
    assert backups_dir() == custom_dir.resolve() / "backups"
    assert logs_dir() == custom_dir.resolve() / "logs"


def test_runtime_backup_zip_contains_db_and_downloads(qt_app) -> None:
    ensure_data_dirs()
    init_db()
    download_file = downloads_dir() / "clip.mp4"
    download_file.write_text("demo", encoding="utf-8")

    from nicheflow_studio.app.main_window import MainWindow

    window = MainWindow()
    try:
        backup_path = window._create_runtime_backup()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()

    assert backup_path.exists() is True
    assert backup_path.parent == backups_dir()
    with zipfile.ZipFile(backup_path) as archive:
        names = set(archive.namelist())

    assert "nicheflow.db" in names
    assert "downloads/clip.mp4" in names


def test_runtime_backup_can_be_restored(qt_app) -> None:
    ensure_data_dirs()
    init_db()

    from nicheflow_studio.app.main_window import MainWindow
    from nicheflow_studio.db.models import Account
    from nicheflow_studio.db.session import get_session, reset_db_state

    original_download = downloads_dir() / "clip.mp4"
    original_download.write_text("original", encoding="utf-8")

    with get_session() as session:
        session.add(Account(name="Original Account", platform="youtube"))
        session.commit()

    window = MainWindow()
    try:
        backup_path = window._create_runtime_backup()

        reset_db_state()
        original_download.write_text("modified", encoding="utf-8")
        init_db()
        with get_session() as session:
            session.add(Account(name="Modified Account", platform="youtube"))
            session.commit()

        window._restore_runtime_backup(backup_path)

        with get_session() as session:
            account_names = [account.name for account in session.query(Account).order_by(Account.name).all()]

        assert account_names == ["Original Account"]
        assert original_download.read_text(encoding="utf-8") == "original"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_init_db_adds_missing_review_and_assignment_columns_for_existing_db() -> None:
    db_dir = Path.cwd() / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "nicheflow.db"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE download_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                source_url VARCHAR(2048) NOT NULL,
                extractor VARCHAR(64),
                video_id VARCHAR(128),
                title VARCHAR(512),
                file_path VARCHAR(2048),
                status VARCHAR(32)
            )
            """
        )
        connection.commit()

    init_db()

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(download_items)").fetchall()
        }

    assert "error_message" in columns
    assert "transcript_text" in columns
    assert "title_draft" in columns
    assert "caption_draft" in columns
    assert "title_style_preset" in columns
    assert "caption_style_preset" in columns
    assert "title_style_config" in columns
    assert "caption_style_config" in columns
    assert "smart_provider_label" in columns
    assert "smart_generation_meta" in columns
    assert "smart_vision_payload" in columns
    assert "smart_generated_at" in columns
    assert "review_state" in columns
    assert "account_id" in columns


def test_init_db_creates_accounts_table() -> None:
    init_db()
    db_path = Path.cwd() / "data" / "nicheflow.db"

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert "accounts" in tables


def test_init_db_adds_scrape_columns_and_creates_candidates_table() -> None:
    db_dir = Path.cwd() / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "nicheflow.db"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                name VARCHAR(128),
                platform VARCHAR(32),
                niche_label VARCHAR(128),
                login_identifier VARCHAR(256),
                credential_blob VARCHAR(2048)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE download_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                source_url VARCHAR(2048) NOT NULL,
                extractor VARCHAR(64),
                video_id VARCHAR(128),
                title VARCHAR(512),
                file_path VARCHAR(2048),
                error_message VARCHAR(512),
                review_state VARCHAR(32),
                account_id INTEGER,
                status VARCHAR(32)
            )
            """
        )
        connection.commit()

    init_db()

    with sqlite3.connect(db_path) as connection:
        account_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert "scrape_source_urls" in account_columns
    assert "scrape_max_items" in account_columns
    assert "scrape_max_age_days" in account_columns
    assert "discovery_keywords" in account_columns
    assert "discovery_mode" in account_columns
    assert "auto_queue_limit" in account_columns
    assert "min_view_count" in account_columns
    assert "min_like_count" in account_columns
    assert "ranking_weight_views" in account_columns
    assert "ranking_weight_likes" in account_columns
    assert "ranking_weight_recency" in account_columns
    assert "ranking_weight_keyword_match" in account_columns
    assert "writing_tone" in account_columns
    assert "target_audience" in account_columns
    assert "hook_style" in account_columns
    assert "banned_phrases" in account_columns
    assert "title_style_notes" in account_columns
    assert "caption_style_notes" in account_columns
    assert "scrape_candidates" in tables
    assert "sources" in tables
    assert "scrape_runs" in tables

    with sqlite3.connect(db_path) as connection:
        candidate_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(scrape_candidates)").fetchall()
        }

    assert "description" in candidate_columns
    assert "view_count" in candidate_columns
    assert "like_count" in candidate_columns
    assert "duration_seconds" in candidate_columns
    assert "thumbnail_url" in candidate_columns
    assert "discovery_query" in candidate_columns
    assert "match_reason" in candidate_columns
    assert "ranking_score" in candidate_columns
    assert "queued_download_item_id" in candidate_columns
    assert "source_id" in candidate_columns
    assert "scrape_run_id" in candidate_columns


def test_init_db_migrates_account_source_urls_into_sources_table() -> None:
    db_dir = Path.cwd() / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "nicheflow.db"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                name VARCHAR(128),
                platform VARCHAR(32),
                niche_label VARCHAR(128),
                login_identifier VARCHAR(256),
                credential_blob VARCHAR(2048),
                scrape_source_urls VARCHAR(4096)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE download_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                source_url VARCHAR(2048) NOT NULL,
                extractor VARCHAR(64),
                video_id VARCHAR(128),
                title VARCHAR(512),
                file_path VARCHAR(2048),
                error_message VARCHAR(512),
                review_state VARCHAR(32),
                account_id INTEGER,
                status VARCHAR(32)
            )
            """
        )
        connection.execute(
            "INSERT INTO accounts (name, platform, scrape_source_urls) VALUES (?, ?, ?)",
            ("YT Main", "youtube", "https://www.youtube.com/@clips"),
        )
        connection.commit()

    init_db()

    with sqlite3.connect(db_path) as connection:
        source_rows = connection.execute(
            "SELECT account_id, source_type, source_url FROM sources"
        ).fetchall()

    assert source_rows == [(1, "youtube_profile", "https://www.youtube.com/@clips")]
