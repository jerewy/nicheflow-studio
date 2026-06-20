PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    account_key TEXT PRIMARY KEY,
    instagram_user_id TEXT NOT NULL,
    token_secret_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    daily_limit INTEGER NOT NULL DEFAULT 6,
    min_gap_minutes INTEGER NOT NULL DEFAULT 240,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_jobs (
    id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    account_key TEXT NOT NULL REFERENCES accounts(account_key),
    caption TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    media_key TEXT NOT NULL UNIQUE,
    media_token TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL DEFAULT 'video/mp4',
    media_size_bytes INTEGER NOT NULL DEFAULT 0,
    meta_container_id TEXT,
    processing_started_at TEXT,
    meta_media_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(account_key, external_id)
);

CREATE INDEX IF NOT EXISTS idx_publish_jobs_due
ON publish_jobs(status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_publish_jobs_account_published
ON publish_jobs(account_key, published_at);

CREATE INDEX IF NOT EXISTS idx_publish_jobs_media_token
ON publish_jobs(media_token);
