"""pywebview bridge for the React Processing screen.

This is the in-process boundary between the React frontend and the Python
draft-revision service (``docs/UI_MIGRATION_PLAN.md``). pywebview exposes an
instance of :class:`ProcessingBridge` to JavaScript as ``window.pywebview.api``;
each public method is callable from React and returns a JSON-serializable
envelope.

Design rules:

- The bridge owns **no** business logic — it only adapts
  ``nicheflow_studio.services.draft_revisions`` calls into envelopes. This keeps
  it symmetric with the Codex CLI (both are thin adapters over one service).
- Every method returns ``{"ok": True, "data": ...}`` or
  ``{"ok": False, "error": "<message>"}`` and never raises, so a backend error
  surfaces as a handled message in the UI instead of breaking the bridge.
- The class has no pywebview import, so it is unit-testable without a window.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from nicheflow_studio.app.local_media import media_url
from nicheflow_studio.core.ui_prefs import set_ui_pref
from nicheflow_studio.db.models import DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import (
    accounts as accounts_svc,
    draft_generation,
    draft_handoff,
    draft_revisions as svc,
    export as export_svc,
    library as library_svc,
    pooling,
    publish_now as publish_now_svc,
    publish_queue,
    publishing_dashboard,
    publishing,
    processing_workflow,
    scraping,
    sourcing,
)
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.services.jobs import JobManager

logger = logging.getLogger(__name__)


def _ok(data: object = None) -> dict:
    return {"ok": True, "data": data}


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


def _cache_busted(url: str | None, path: str | None) -> str | None:
    """Append the file's mtime so a file re-rendered at the same path isn't served
    stale from the WebView cache — e.g. a re-export with a new manual crop writes
    the same ``processed_path``, so without this the preview shows the old video."""
    if not url or not path:
        return url
    try:
        token = int(Path(path).stat().st_mtime)
    except OSError:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={token}"


def _guard(func):
    """Wrap a bridge call so service errors become envelopes, never exceptions.

    Known :class:`DraftRevisionError` cases pass their message through verbatim;
    anything unexpected is logged with a stack trace and returned as a generic
    message so we never leak internals to the UI.
    """

    def wrapper(*args, **kwargs) -> dict:
        try:
            return _ok(func(*args, **kwargs))
        except ServiceError as exc:
            return _fail(str(exc))
        except Exception:  # noqa: BLE001 - bridge boundary must not propagate
            logger.exception("Unexpected error in bridge call %s", getattr(func, "__name__", "?"))
            return _fail("Unexpected error. Check the application logs.")

    return wrapper


class ProcessingBridge:
    """Methods exposed to the React Processing screen via pywebview."""

    def __init__(self, media_ready: threading.Event | None = None) -> None:
        # One job manager per window; tracks background generation/export work.
        self._jobs = JobManager()
        self._media_ready = media_ready

    @_guard
    def list_items(self) -> list[dict]:
        """Recent downloaded items the user can select to process."""
        return publishing.list_items()

    # --- Account Manager (migrated screen) --- #

    @_guard
    def list_accounts(self) -> list[dict]:
        return accounts_svc.list_accounts()

    @_guard
    def get_account(self, account_id: int) -> dict:
        return accounts_svc.get_account(account_id)

    @_guard
    def create_account(self, payload: dict | None = None) -> dict:
        return accounts_svc.create_account(payload or {})

    @_guard
    def update_account(self, account_id: int, payload: dict | None = None) -> dict:
        return accounts_svc.update_account(account_id, payload or {})

    @_guard
    def delete_account(self, account_id: int) -> dict:
        return accounts_svc.delete_account(account_id)

    @_guard
    def get_active_account(self) -> dict:
        return accounts_svc.get_active_account()

    @_guard
    def set_active_account(self, account_id: int | None = None) -> dict:
        return accounts_svc.set_active_account(account_id)

    # --- Library / Processing item list --- #

    @_guard
    def list_library_items(self, account_id: int | None = None) -> list[dict]:
        return library_svc.list_items(account_id)

    @_guard
    def assign_account(self, item_id: int, account_id: int | None = None) -> dict:
        return library_svc.assign_account(item_id, account_id)

    @_guard
    def remove_library_item(self, item_id: int) -> dict:
        return library_svc.remove_item(item_id)

    @_guard
    def remove_item_from_pool(self, item_id: int, reason: str = "manual removal") -> dict:
        return library_svc.remove_item_from_pool(item_id, reason)

    @_guard
    def reject_item(self, item_id: int, reason: str = "low_quality") -> dict:
        return library_svc.reject_item(item_id, reason)

    @_guard
    def reject_item_globally(self, item_id: int, reason: str = "globally rejected") -> dict:
        return library_svc.reject_item_globally(item_id, reason)

    # --- Publishing Dashboard / Publish Queue (migrated screen) --- #

    @_guard
    def list_publish_queue(self, account_id: int | None = None) -> list[dict]:
        return publish_queue.list_jobs(account_id)

    @_guard
    def mark_job_posted(self, job_id: int, payload: dict | None = None) -> dict:
        return publish_queue.mark_posted(job_id, payload or {})

    @_guard
    def update_job_metrics(self, job_id: int, payload: dict | None = None) -> dict:
        return publish_queue.update_metrics(job_id, payload or {})

    @_guard
    def reschedule_job(self, job_id: int, scheduled_at: str) -> dict:
        return publish_queue.reschedule(job_id, scheduled_at)

    @_guard
    def unschedule_job(self, job_id: int) -> dict:
        return publish_queue.unschedule(job_id)

    @_guard
    def remove_publish_job(self, job_id: int) -> dict:
        return publish_queue.remove_job(job_id)

    # --- Scraping / Sources (per active account) --- #

    @_guard
    def list_sources(self, account_id: int) -> list[dict]:
        return sourcing.list_sources(account_id)

    @_guard
    def add_source(self, account_id: int, source_url: str) -> dict:
        return sourcing.add_source(account_id, source_url)

    @_guard
    def set_source_enabled(self, source_id: int, enabled: bool) -> dict:
        return sourcing.set_source_enabled(source_id, enabled)

    @_guard
    def remove_source(self, source_id: int) -> dict:
        return sourcing.remove_source(source_id)

    @_guard
    def list_candidates(self, account_id: int, state: str = "all") -> list[dict]:
        return sourcing.list_candidates(account_id, state)

    @_guard
    def set_candidate_state(self, candidate_id: int, state: str) -> dict:
        return sourcing.set_candidate_state(candidate_id, state)

    @_guard
    def accept_candidate(self, candidate_id: int) -> dict:
        return sourcing.accept_candidate(candidate_id)

    @_guard
    def reject_candidate(self, candidate_id: int, reason: str = "low_quality") -> dict:
        return sourcing.reject_candidate(candidate_id, reason)

    @_guard
    def candidate_preview(self, candidate_id: int) -> dict:
        """Preview source for a candidate: a local video URL if its footage is
        downloaded, else the (possibly expired) scraped thumbnail."""
        data = sourcing.candidate_preview(candidate_id)
        mapping_ready = self._media_ready is None or self._media_ready.wait(timeout=5)
        data["preview_url"] = media_url(data.get("local_path")) if mapping_ready else None
        return data

    @_guard
    def apify_usage(self) -> dict:
        """This month's Apify free-tier usage, for the Scraping-tab reminder."""
        return scraping.apify_usage()

    @_guard
    def start_source_scrape(self, source_id: int, max_items: int = 30) -> dict:
        """Start a background Apify scrape of a source into its niche pool."""
        job_id = self._jobs.start(scraping.scrape_source_to_pool, source_id, max_items=max_items)
        return {"job_id": job_id}

    @_guard
    def start_candidate_download(self, candidate_id: int) -> dict:
        """Start a background job to add a candidate into Processing (reuse the
        file if on disk, else download it). Poll the job via :meth:`get_job`."""
        job_id = self._jobs.start(scraping.add_candidate_to_processing, candidate_id)
        return {"job_id": job_id}

    # --- Pooling / Distribution (migrated screen, read-only) --- #

    @_guard
    def pooling_overview(self) -> dict:
        return pooling.overview()

    @_guard
    def list_pool_items(self, niche: str) -> list[dict]:
        return pooling.list_pool_items(niche)

    @_guard
    def list_pool_sources(self, niche: str) -> list[dict]:
        return pooling.list_sources(niche)

    @_guard
    def list_pool_source_clips(
        self, niche: str, source_label: str, include_removed: bool = False
    ) -> list[dict]:
        clips = pooling.list_source_clips(niche, source_label, include_removed=include_removed)
        # Same virtual-host gating as get_context: only hand out media URLs once
        # the WebView folder mapping is installed.
        mapping_ready = self._media_ready is None or self._media_ready.wait(timeout=5)
        for clip in clips:
            clip["preview_url"] = (
                media_url(clip.get("original_download_path")) if mapping_ready else None
            )
        return clips

    @_guard
    def remove_pool_item(self, pool_item_id: int, reason: str = "manual removal") -> dict:
        return pooling.remove_pool_item(pool_item_id, reason)

    @_guard
    def restore_pool_item(self, pool_item_id: int) -> dict:
        return pooling.restore_pool_item(pool_item_id)

    @_guard
    def pool_niche_accounts(self, niche: str) -> list[dict]:
        return pooling.niche_accounts(niche)

    @_guard
    def distribute_pool_item(self, pool_item_id: int, account_ids: list[int] | None = None) -> dict:
        return pooling.distribute_clip(pool_item_id, account_ids or [])

    @_guard
    def distribute_niche(self, niche: str, max_per_account: int | None = None) -> dict:
        """Auto-distribute the niche's undistributed pool across its accounts,
        engagement-ranked (likes + recency)."""
        return pooling.distribute_niche(niche, max_per_account)

    @_guard
    def distribute_niche_explicit(self, niche: str, targets: dict | None = None) -> dict:
        return pooling.distribute_niche_explicit(niche, targets or {})

    @_guard
    def dashboard_publish_jobs(self) -> dict:
        return publishing_dashboard.list_global_publish_jobs()

    @_guard
    def dashboard_mark_ready(self, job_ids: list[int] | None = None) -> dict:
        return publishing_dashboard.mark_ready(job_ids or [])

    @_guard
    def dashboard_open_output(self, job_id: int) -> dict:
        return publishing_dashboard.open_output(job_id)

    @_guard
    def dashboard_account_readiness(self) -> dict:
        return publishing_dashboard.account_readiness()

    @_guard
    def dashboard_start_live_health_check(self) -> dict:
        return {"job_id": self._jobs.start(publishing_dashboard.check_all_live)}

    @_guard
    def dashboard_relogin(self, account_id: int) -> dict:
        return publishing_dashboard.relogin(account_id)

    @_guard
    def get_context(self, item_id: int | None = None) -> dict:
        """Active Processing context for ``item_id`` (or the resolved current item)."""
        if item_id is not None:
            with get_session() as session:
                pending = session.get(DownloadItem, item_id)
                should_download = bool(
                    pending and pending.status == "pending_review" and not pending.file_path
                )
            if should_download:
                library_svc.ensure_item_downloaded(item_id)
        context = svc.active_context(item_id)
        item = context["item"]
        mapping_ready = self._media_ready is None or self._media_ready.wait(timeout=5)
        item["original_preview_url"] = media_url(item.get("file_path")) if mapping_ready else None
        exported_url = media_url(item.get("processed_path")) if mapping_ready else None
        # Cache-bust so re-exports (e.g. after a manual crop change) aren't served
        # stale from the WebView cache at the same processed_path.
        item["exported_preview_url"] = _cache_busted(exported_url, item.get("processed_path"))
        item["preview_url"] = item["exported_preview_url"] or item["original_preview_url"]
        # Opening an item clears its NEW badge (best-effort; never block the read).
        try:
            library_svc.mark_seen(item["id"])
        except Exception:  # noqa: BLE001
            logger.exception("mark_seen failed for item %s", item.get("id"))
        return context

    @_guard
    def get_latest_revision(self, item_id: int) -> dict | None:
        """Latest draft revision for an item, or ``None`` when there are none."""
        latest = svc.latest_revision(item_id)
        return latest.to_dict() if latest is not None else None

    @_guard
    def list_revisions(self, item_id: int) -> list[dict]:
        return [dto.to_dict() for dto in svc.list_revisions(item_id)]

    @_guard
    def save_revision(self, item_id: int, payload: dict | None = None) -> dict:
        payload = payload or {}
        dto = svc.save_revision(
            item_id,
            title_options=payload.get("title_options") or [],
            caption_options=payload.get("caption_options") or [],
            summary=payload.get("summary"),
            option_notes=payload.get("option_notes"),
            option_tiers=payload.get("option_tiers"),
            recommended_title_index=payload.get("recommended_title_index"),
            recommended_caption_index=payload.get("recommended_caption_index"),
            recommendation_reason=payload.get("recommendation_reason"),
            title_style_preset=payload.get("title_style_preset"),
            caption_style_preset=payload.get("caption_style_preset"),
            provider_label=payload.get("provider_label"),
            generation_meta=payload.get("generation_meta"),
            vision_payload=payload.get("vision_payload"),
            source=payload.get("source") or "ui",
        )
        return dto.to_dict()

    @_guard
    def revise_option(self, item_id: int, option_number: int, payload: dict | None = None) -> dict:
        payload = payload or {}
        dto = svc.revise_option(
            item_id,
            option_number,
            title=payload.get("title"),
            caption=payload.get("caption"),
            note=payload.get("note"),
            revision_id=payload.get("revision_id"),
            source=payload.get("source") or "ui",
        )
        return dto.to_dict()

    @_guard
    def apply_revision(
        self, item_id: int, option_number: int, revision_id: int | None = None
    ) -> dict:
        return svc.apply_revision(item_id, option_number, revision_id=revision_id)

    @_guard
    def set_active_item(self, item_id: int) -> dict:
        """Persist which item the user is editing so the Codex CLI ``current``
        command follows the UI selection."""
        set_ui_pref(svc.ACTIVE_PROCESSING_ITEM_PREF_KEY, int(item_id))
        return {"active_processing_item_id": int(item_id)}

    @_guard
    def can_generate(self) -> dict:
        """Whether a draft provider (Groq/Ollama) is configured, so the UI can
        enable/disable the Generate action."""
        return {"can_generate": draft_generation.can_generate()}

    @_guard
    def build_chat_prompt(self, item_id: int, payload: dict | None = None) -> dict:
        return {"prompt": draft_handoff.build_chat_prompt(item_id, payload)}

    @_guard
    def import_pasted_draft(self, item_id: int, text: str) -> dict:
        return draft_handoff.import_pasted_draft(item_id, text).to_dict()

    @_guard
    def get_workflow_settings(self, item_id: int) -> dict:
        return processing_workflow.get_settings(item_id)

    @_guard
    def save_workflow_settings(self, item_id: int, payload: dict | None = None) -> dict:
        return processing_workflow.save_settings(item_id, payload or {})

    @_guard
    def save_final_draft(self, item_id: int, title: str, caption: str) -> dict:
        return processing_workflow.save_final_draft(item_id, title, caption)

    @_guard
    def open_item_folder(self, item_id: int) -> dict:
        return processing_workflow.open_folder(item_id)

    @_guard
    def get_crop_override(self, item_id: int) -> dict | None:
        """The item's manual crop keep-region, or ``None`` if it auto-crops."""
        return export_svc.get_crop_override(item_id)

    @_guard
    def get_crop_preview(self, item_id: int) -> dict:
        """A still-frame URL for the crop editor; avoids WebView video crashes."""
        preview_path = export_svc.crop_preview_frame(item_id)
        preview_url = media_url(str(preview_path))
        if preview_url is None:
            raise ServiceError("Could not expose the crop preview to the Processing window.")
        return {"item_id": item_id, "preview_url": preview_url}

    @_guard
    def save_crop_override(self, item_id: int, payload: dict | None = None) -> dict:
        return export_svc.save_crop_override(item_id, payload or {})

    @_guard
    def clear_crop_override(self, item_id: int) -> dict:
        return export_svc.clear_crop_override(item_id)

    @_guard
    def start_generation(self, item_id: int, payload: dict | None = None) -> dict:
        """Start background draft generation; return a job id to poll via
        :meth:`get_job`. The job result is the saved revision as a dict."""
        payload = payload or {}

        def _run() -> dict:
            dto = draft_generation.generate_revision_for_item(
                item_id,
                caption_style=payload.get("caption_style"),
                title_style=payload.get("title_style"),
                prompt_profile=payload.get("prompt_profile"),
                clip_premise=payload.get("clip_premise"),
                source=payload.get("source") or "ui",
            )
            return dto.to_dict()

        job_id = self._jobs.start(_run)
        return {"job_id": job_id}

    @_guard
    def start_export(self, item_id: int) -> dict:
        """Start background Reel export; return a job id to poll via
        :meth:`get_job`. The job reports progress and its result is
        ``{item_id, processed_path}``."""
        job_id = self._jobs.start(export_svc.export_item, item_id)
        return {"job_id": job_id}

    @_guard
    def get_job(self, job_id: str) -> dict:
        """Status snapshot for a background job started via the bridge."""
        snapshot = self._jobs.get(job_id)
        if snapshot is None:
            raise svc.DraftRevisionError(f"Unknown job id {job_id}.")
        return snapshot

    @_guard
    def list_publish_jobs(self, item_id: int) -> list[dict]:
        """Publish-queue rows linked to an item."""
        return publishing.list_publish_jobs(item_id)

    @_guard
    def queue_for_publish(self, item_id: int, scheduled_at: str | None = None) -> dict:
        """Add/update the item's exported reel in the publish queue (draft, or
        scheduled when ``scheduled_at`` is given)."""
        return publishing.queue_for_publish(item_id, scheduled_at=scheduled_at)

    @_guard
    def start_publish_now(self, item_id: int) -> dict:
        """Start a background job that posts the item's reel to Instagram now
        (live). Poll it via :meth:`get_job`."""
        job_id = self._jobs.start(publish_now_svc.publish_item_now, item_id)
        return {"job_id": job_id}

    @_guard
    def publish_due_count(self) -> dict:
        """How many scheduled jobs are currently due (past their time)."""
        return {"due": publish_now_svc.due_count()}

    @_guard
    def start_publish_due(self) -> dict:
        """Start a background job that posts all currently-due scheduled reels."""
        job_id = self._jobs.start(publish_now_svc.publish_due_jobs)
        return {"job_id": job_id}

    @_guard
    def get_auto_publish(self) -> dict:
        return {"enabled": publish_now_svc.auto_publish_enabled()}

    @_guard
    def set_auto_publish(self, enabled: bool = False) -> dict:
        return {"enabled": publish_now_svc.set_auto_publish_enabled(enabled)}

    @_guard
    def auto_schedule_for_publish(self, item_id: int) -> dict:
        """Schedule the exported reel in the account's next open posting slot."""
        return publishing.auto_schedule_for_publish(item_id)
