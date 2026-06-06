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

from nicheflow_studio.core.ui_prefs import set_ui_pref
from nicheflow_studio.services import draft_revisions as svc

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
        except svc.DraftRevisionError as exc:
            return _fail(str(exc))
        except Exception:  # noqa: BLE001 - bridge boundary must not propagate
            logger.exception("Unexpected error in bridge call %s", getattr(func, "__name__", "?"))
            return _fail("Unexpected error. Check the application logs.")

    return wrapper


class ProcessingBridge:
    """Methods exposed to the React Processing screen via pywebview."""

    @_guard
    def get_context(self, item_id: int | None = None) -> dict:
        """Active Processing context for ``item_id`` (or the resolved current item)."""
        return svc.active_context(item_id)

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
