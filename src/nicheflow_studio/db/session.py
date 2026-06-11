from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from nicheflow_studio.core.paths import data_dir
from nicheflow_studio.db.models import Account, Base, Source


def _parse_source_urls(raw_value: str | None) -> list[str]:
    if raw_value is None:
        return []
    normalized = raw_value.replace("\n", ",").replace(";", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def _infer_source_type(source_url: str) -> str:
    lowered = source_url.lower()
    if "instagram.com/explore/tags/" in lowered:
        return "instagram_hashtag"
    if "instagram.com/" in lowered:
        return "instagram_profile"
    if "/@" in lowered:
        return "youtube_profile"
    return "youtube_channel"


def _source_label(source_url: str) -> str:
    lowered = source_url.lower()
    if "instagram.com/explore/tags/" in lowered:
        return f"#{source_url.rstrip('/').rsplit('/', 1)[-1]}"
    if "instagram.com/" in lowered:
        return f"@{source_url.rstrip('/').rsplit('/', 1)[-1]}"
    return source_url.rstrip("/").rsplit("/", 1)[-1] or source_url


def _migrate_account_sources() -> None:
    assert _SESSION_FACTORY is not None
    session = _SESSION_FACTORY()
    try:
        accounts = session.query(Account).all()
        for account in accounts:
            source_urls = _parse_source_urls(account.scrape_source_urls)
            if not source_urls:
                continue
            existing_urls = {
                source.source_url
                for source in session.query(Source).filter(Source.account_id == account.id).all()
            }
            created = False
            for source_url in source_urls:
                if source_url in existing_urls:
                    continue
                session.add(
                    Source(
                        account_id=account.id,
                        platform=account.platform,
                        source_type=_infer_source_type(source_url),
                        label=_source_label(source_url),
                        source_url=source_url,
                        enabled=1,
                        priority=100,
                    )
                )
                existing_urls.add(source_url)
                created = True
            if created:
                session.commit()
    finally:
        session.close()


def _backfill_account_niche() -> None:
    """Set the strict ``niche`` for existing accounts from their niche_label.

    Only fills rows where ``niche`` is still NULL, so a manually-set niche is
    never overwritten. Accounts that don't classify (e.g. meme) stay NULL.
    """
    assert _SESSION_FACTORY is not None
    from nicheflow_studio.core.niche import classify_niche

    session = _SESSION_FACTORY()
    try:
        changed = False
        for account in session.query(Account).filter(Account.niche.is_(None)).all():
            inferred = classify_niche(account.niche_label)
            if inferred is not None:
                account.niche = inferred
                changed = True
        if changed:
            session.commit()
    finally:
        session.close()


def database_path() -> Path:
    """Return the resolved runtime SQLite database path."""
    return data_dir() / "nicheflow.db"


_ENGINE = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def _ensure_compatibility() -> None:
    assert _ENGINE is not None

    # Keep the MVP schema forward-compatible without introducing Alembic yet.
    with _ENGINE.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("download_items")}
        if "error_message" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN error_message VARCHAR(512)")
            )
        if "transcript_text" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN transcript_text VARCHAR(65535)")
            )
        if "source_description" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN source_description VARCHAR(8192)")
            )
        if "processed_path" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN processed_path VARCHAR(2048)")
            )
        if "title_draft" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN title_draft VARCHAR(1024)")
            )
        if "caption_draft" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN caption_draft VARCHAR(65535)")
            )
        if "title_style_preset" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN title_style_preset VARCHAR(128)")
            )
        if "caption_style_preset" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN caption_style_preset VARCHAR(128)")
            )
        if "title_style_config" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN title_style_config VARCHAR(4096)")
            )
        if "caption_style_config" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN caption_style_config VARCHAR(4096)")
            )
        if "smart_summary" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN smart_summary VARCHAR(4096)")
            )
        if "smart_title_options" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN smart_title_options VARCHAR(8192)")
            )
        if "smart_caption_options" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN smart_caption_options VARCHAR(16384)")
            )
        if "smart_provider_label" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN smart_provider_label VARCHAR(256)")
            )
        if "smart_generation_meta" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN smart_generation_meta VARCHAR(4096)")
            )
        if "smart_vision_payload" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN smart_vision_payload VARCHAR(8192)")
            )
        if "smart_generated_at" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN smart_generated_at DATETIME")
            )
        if "review_state" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN review_state VARCHAR(32) DEFAULT 'new'")
            )
        if "account_id" not in columns:
            connection.execute(text("ALTER TABLE download_items ADD COLUMN account_id INTEGER"))
        if "crop_override" not in columns:
            connection.execute(
                text("ALTER TABLE download_items ADD COLUMN crop_override VARCHAR(256)")
            )
        if "seen_at" not in columns:
            connection.execute(text("ALTER TABLE download_items ADD COLUMN seen_at DATETIME"))

        account_columns = {column["name"] for column in inspect(connection).get_columns("accounts")}
        if "scrape_source_urls" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN scrape_source_urls VARCHAR(4096)")
            )
        if "scrape_max_items" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN scrape_max_items INTEGER"))
        if "scrape_max_age_days" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN scrape_max_age_days INTEGER"))
        if "discovery_keywords" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN discovery_keywords VARCHAR(4096)")
            )
        if "discovery_mode" not in account_columns:
            connection.execute(
                text(
                    "ALTER TABLE accounts ADD COLUMN discovery_mode VARCHAR(32) DEFAULT 'review_only'"
                )
            )
        if "auto_queue_limit" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN auto_queue_limit INTEGER"))
        if "min_view_count" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN min_view_count INTEGER"))
        if "min_like_count" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN min_like_count INTEGER"))
        if "candidate_min_like_filter" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN candidate_min_like_filter INTEGER")
            )
        if "ranking_weight_views" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN ranking_weight_views INTEGER"))
        if "ranking_weight_likes" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN ranking_weight_likes INTEGER"))
        if "ranking_weight_recency" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN ranking_weight_recency INTEGER")
            )
        if "ranking_weight_keyword_match" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN ranking_weight_keyword_match INTEGER")
            )
        if "writing_tone" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN writing_tone VARCHAR(256)"))
        if "target_audience" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN target_audience VARCHAR(256)"))
        if "instagram_profile" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN instagram_profile VARCHAR(64)"))
        if "instagram_handle" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN instagram_handle VARCHAR(64)"))
        if "hook_style" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN hook_style VARCHAR(256)"))
        if "banned_phrases" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN banned_phrases VARCHAR(2048)"))
        if "title_style_notes" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN title_style_notes VARCHAR(2048)")
            )
        if "caption_style_notes" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN caption_style_notes VARCHAR(2048)")
            )
        if "upload_timezone" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN upload_timezone VARCHAR(64)"))
        if "upload_default_privacy" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN upload_default_privacy VARCHAR(32)")
            )
        if "upload_schedule_slots" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN upload_schedule_slots VARCHAR(512)")
            )
        if "daily_posts_target" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN daily_posts_target INTEGER"))
        if "auto_schedule_on_export" not in account_columns:
            connection.execute(
                text(
                    "ALTER TABLE accounts ADD COLUMN auto_schedule_on_export "
                    "BOOLEAN DEFAULT 0"
                )
            )
        if "upload_made_for_kids" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN upload_made_for_kids INTEGER DEFAULT 0")
            )
        if "upload_contains_synthetic_media" not in account_columns:
            connection.execute(
                text(
                    "ALTER TABLE accounts ADD COLUMN upload_contains_synthetic_media INTEGER DEFAULT 0"
                )
            )
        if "processing_preferences" not in account_columns:
            connection.execute(
                text("ALTER TABLE accounts ADD COLUMN processing_preferences VARCHAR(4096)")
            )
        if "niche" not in account_columns:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN niche VARCHAR(16)"))

        if "pool_items" in inspect(connection).get_table_names():
            pool_item_columns = {
                column["name"] for column in inspect(connection).get_columns("pool_items")
            }
            if "pinned_account_id" not in pool_item_columns:
                connection.execute(
                    text("ALTER TABLE pool_items ADD COLUMN pinned_account_id INTEGER")
                )

        upload_job_columns = {
            column["name"] for column in inspect(connection).get_columns("upload_jobs")
        }
        if "posted_url" not in upload_job_columns:
            connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_url VARCHAR(2048)"))
        if "posted_at" not in upload_job_columns:
            connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_at DATETIME"))
        if "posted_views" not in upload_job_columns:
            connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_views INTEGER"))
        if "posted_likes" not in upload_job_columns:
            connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_likes INTEGER"))
        if "posted_comments" not in upload_job_columns:
            connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_comments INTEGER"))
        if "posted_shares" not in upload_job_columns:
            connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_shares INTEGER"))
        if "content_type" not in upload_job_columns:
            connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN content_type VARCHAR(64)"))

        candidate_columns = {
            column["name"] for column in inspect(connection).get_columns("scrape_candidates")
        }
        if "description" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN description VARCHAR(8192)")
            )
        if "view_count" not in candidate_columns:
            connection.execute(text("ALTER TABLE scrape_candidates ADD COLUMN view_count INTEGER"))
        if "like_count" not in candidate_columns:
            connection.execute(text("ALTER TABLE scrape_candidates ADD COLUMN like_count INTEGER"))
        if "comment_count" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN comment_count INTEGER")
            )
        if "duration_seconds" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN duration_seconds INTEGER")
            )
        if "thumbnail_url" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN thumbnail_url VARCHAR(2048)")
            )
        if "discovery_query" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN discovery_query VARCHAR(512)")
            )
        if "match_reason" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN match_reason VARCHAR(256)")
            )
        if "ranking_score" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN ranking_score INTEGER")
            )
        if "queued_download_item_id" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN queued_download_item_id INTEGER")
            )
        if "source_id" not in candidate_columns:
            connection.execute(text("ALTER TABLE scrape_candidates ADD COLUMN source_id INTEGER"))
        if "scrape_run_id" not in candidate_columns:
            connection.execute(
                text("ALTER TABLE scrape_candidates ADD COLUMN scrape_run_id INTEGER")
            )

        # Content dedup fingerprint (additive). media_assets is created by
        # create_all on fresh DBs (already has the column); this backfills it on
        # databases created before content dedup existed.
        if "media_assets" in inspect(connection).get_table_names():
            media_asset_columns = {
                column["name"] for column in inspect(connection).get_columns("media_assets")
            }
            if "content_hash" not in media_asset_columns:
                connection.execute(
                    text("ALTER TABLE media_assets ADD COLUMN content_hash VARCHAR(1024)")
                )


def init_db() -> None:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        return

    db_path = database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _ENGINE = create_engine(f"sqlite:///{db_path}", future=True)
    _SESSION_FACTORY = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(_ENGINE)
    _ensure_compatibility()
    _migrate_account_sources()
    _backfill_account_niche()


def reset_db_state() -> None:
    global _ENGINE, _SESSION_FACTORY

    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None


@contextmanager
def get_session() -> Session:
    if _SESSION_FACTORY is None:
        init_db()

    assert _SESSION_FACTORY is not None
    session = _SESSION_FACTORY()
    try:
        yield session
    finally:
        session.close()
