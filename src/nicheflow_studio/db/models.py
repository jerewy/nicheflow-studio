from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )

    name: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(32), default="youtube")
    niche_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Strict niche for the shared-pool network: "history" | "movie" (None until
    # classified). Drives pool isolation so history content never lands on a
    # movie account and vice versa. Distinct from the free-text niche_label,
    # which stays for display/prompt context. See docs/SOURCING_POOLING_PLAN.md.
    niche: Mapped[str | None] = mapped_column(String(16), nullable=True)
    login_identifier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    instagram_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Expected Instagram @handle for this account. When set, the live health
    # check verifies the logged-in session is actually this account and flags a
    # mismatch (wrong account in the profile) so the user can re-login.
    instagram_handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_blob: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scrape_source_urls: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    scrape_max_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scrape_max_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovery_keywords: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    discovery_mode: Mapped[str] = mapped_column(String(32), default="review_only")
    auto_queue_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_min_like_filter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranking_weight_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranking_weight_likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranking_weight_recency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranking_weight_keyword_match: Mapped[int | None] = mapped_column(Integer, nullable=True)
    writing_tone: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_audience: Mapped[str | None] = mapped_column(String(256), nullable=True)
    hook_style: Mapped[str | None] = mapped_column(String(256), nullable=True)
    banned_phrases: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title_style_notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    caption_style_notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    upload_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upload_default_privacy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    upload_schedule_slots: Mapped[str | None] = mapped_column(String(512), nullable=True)
    upload_made_for_kids: Mapped[int] = mapped_column(Integer, default=0)
    upload_contains_synthetic_media: Mapped[int] = mapped_column(Integer, default=0)
    # JSON snapshot of the user's last-used Processing settings for this
    # account (template, caption_style, prompt title_style, visual title
    # style preset, font_name, font_size, text_color, background, layout).
    # Auto-saved on widget change and re-applied when the account is
    # selected, so users don't have to re-pick niche-appropriate styles
    # every time they switch accounts.
    processing_preferences: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    download_items: Mapped[list["DownloadItem"]] = relationship(back_populates="account")
    upload_jobs: Mapped[list["UploadJob"]] = relationship(back_populates="account")
    sources: Mapped[list["Source"]] = relationship(back_populates="account")
    scrape_runs: Mapped[list["ScrapeRun"]] = relationship(back_populates="account")
    scrape_candidates: Mapped[list["ScrapeCandidate"]] = relationship(back_populates="account")


class DownloadItem(Base):
    __tablename__ = "download_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    source_url: Mapped[str] = mapped_column(String(2048))
    extractor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_description: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    processed_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(String(65535), nullable=True)
    title_draft: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    caption_draft: Mapped[str | None] = mapped_column(String(65535), nullable=True)
    title_style_preset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    caption_style_preset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title_style_config: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    caption_style_config: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    smart_summary: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    smart_title_options: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    smart_caption_options: Mapped[str | None] = mapped_column(String(16384), nullable=True)
    smart_provider_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    smart_generation_meta: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    smart_vision_payload: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    smart_generated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_state: Mapped[str] = mapped_column(String(32), default="new")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")

    account: Mapped[Account | None] = relationship(back_populates="download_items")
    upload_jobs: Mapped[list["UploadJob"]] = relationship(back_populates="download_item")


class UploadJob(Base):
    __tablename__ = "upload_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    download_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_items.id"), nullable=True
    )
    processed_path: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(String(65535), nullable=True)
    tags: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    privacy_status: Mapped[str] = mapped_column(String(32), default="private")
    made_for_kids: Mapped[int] = mapped_column(Integer, default=0)
    contains_synthetic_media: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    youtube_video_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    posted_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)

    account: Mapped[Account] = relationship(back_populates="upload_jobs")
    download_item: Mapped[DownloadItem | None] = relationship(back_populates="upload_jobs")


class ScrapeCandidate(Base):
    __tablename__ = "scrape_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )

    scrape_source_url: Mapped[str] = mapped_column(String(2048))
    source_url: Mapped[str] = mapped_column(String(2048))
    extractor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    channel_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    discovery_query: Mapped[str | None] = mapped_column(String(512), nullable=True)
    match_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ranking_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="candidate")
    queued_download_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_items.id"),
        nullable=True,
    )
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    scrape_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))

    account: Mapped[Account] = relationship(back_populates="scrape_candidates")
    source: Mapped["Source | None"] = relationship(back_populates="candidates")
    scrape_run: Mapped["ScrapeRun | None"] = relationship(back_populates="candidates")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    platform: Mapped[str] = mapped_column(String(32), default="youtube")
    source_type: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(256))
    source_url: Mapped[str] = mapped_column(String(2048))
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    last_scraped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)

    account: Mapped[Account] = relationship(back_populates="sources")
    candidates: Mapped[list[ScrapeCandidate]] = relationship(back_populates="source")
    scrape_runs: Mapped[list["ScrapeRun"]] = relationship(back_populates="source")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    items_fetched: Mapped[int] = mapped_column(Integer, default=0)
    items_accepted: Mapped[int] = mapped_column(Integer, default=0)
    items_rejected: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))

    account: Mapped[Account] = relationship(back_populates="scrape_runs")
    source: Mapped[Source] = relationship(back_populates="scrape_runs")
    candidates: Mapped[list[ScrapeCandidate]] = relationship(back_populates="scrape_run")


class MediaAsset(Base):
    """Global Media Library — one row per ORIGINAL downloaded source video,
    deduplicated across accounts and niches by canonical URL / shortcode.

    This is the dedup gate from docs/SOURCING_POOLING_PLAN.md §5.1/§8: the same
    source clip (re-imported, reused across accounts) is downloaded ONCE and
    referenced many times, instead of one physical download per assignment.
    """

    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    platform: Mapped[str] = mapped_column(String(32), default="instagram")
    # Dedup keys. canonical_source_url is the normalized post/reel URL;
    # source_shortcode is the Instagram shortcode when known (the stronger key).
    canonical_source_url: Mapped[str] = mapped_column(String(2048))
    source_shortcode: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    platform_media_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_download_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "pending" until the original is on disk, then "downloaded".
    download_status: Mapped[str] = mapped_column(String(32), default="pending")
    downloaded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
