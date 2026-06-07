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

from nicheflow_studio.app.local_media import media_url
from nicheflow_studio.core.ui_prefs import set_ui_pref
from nicheflow_studio.services import (
    accounts as accounts_svc,
    draft_generation,
    draft_handoff,
    draft_revisions as svc,
    export as export_svc,
    publishing,
    processing_workflow,
)
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.services.jobs import JobManager

logger = logging.getLogger(__name__)


def _ok(data: object = None) -> dict:
    return {"ok": True, "data": data}


def _fail(message: str) -> dict:
    return {"ok": False, "error": message}


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
    def get_context(self, item_id: int | None = None) -> dict:
        """Active Processing context for ``item_id`` (or the resolved current item)."""
        context = svc.active_context(item_id)
        item = context["item"]
        mapping_ready = self._media_ready is None or self._media_ready.wait(timeout=5)
        item["original_preview_url"] = media_url(item.get("file_path")) if mapping_ready else None
        item["exported_preview_url"] = (
            media_url(item.get("processed_path")) if mapping_ready else None
        )
        item["preview_url"] = item["exported_preview_url"] or item["original_preview_url"]
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
    def auto_schedule_for_publish(self, item_id: int) -> dict:
        """Schedule the exported reel in the account's next open posting slot."""
        return publishing.auto_schedule_for_publish(item_id)
