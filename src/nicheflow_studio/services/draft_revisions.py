"""Shared, UI-independent service for versioned Processing draft revisions.

This is the single source of truth for the database-backed Codex -> UI draft
handoff described in ``docs/UI_MIGRATION_PLAN.md``. Both the Codex repository
CLI (``scripts/nicheflow_drafts.py``) and the future pywebview/React bridge call
into here; neither should re-implement these rules.

Conventions:

- Option numbers in the public API are **1-based** to match the product's
  "Title Option N" / "Recommended Pick: Title Option N" language.
- A "revision" is an immutable snapshot. ``save_revision`` and ``revise_option``
  always insert a *new* row with the next ``revision_number`` rather than
  mutating an existing one, so history stays recoverable.
- ``apply_revision`` is the only operation that touches the live
  ``DownloadItem``: it copies the chosen option onto ``title_draft`` /
  ``caption_draft`` and mirrors the ``smart_*`` fields so the existing
  export/publish path keeps working unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass

from sqlalchemy import select

from nicheflow_studio.core.ui_prefs import get_ui_pref
from nicheflow_studio.db.models import Account, DownloadItem, DraftRevision
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

# UI pref the desktop/React app may set to tell the CLI which item the user is
# actively editing. Absent until the UI writes it; ``current`` falls back to the
# most recently downloaded item when it is missing or stale.
ACTIVE_PROCESSING_ITEM_PREF_KEY = "active_processing_item_id"

# How much transcript text to include in the context payload. Codex only needs
# enough grounding to write drafts, not the full transcript blob.
_CONTEXT_TRANSCRIPT_CHARS = 4000


class DraftRevisionError(ServiceError):
    """Raised for invalid draft-revision operations (bad item, bad option, etc.).

    Callers (CLI, bridge) translate this into a user-facing message; it should
    never leak a stack trace to the user.
    """


@dataclass(frozen=True)
class DraftRevisionDTO:
    """Plain, JSON-serializable view of one stored revision."""

    id: int
    download_item_id: int
    revision_number: int
    source: str
    created_at: str | None
    summary: str | None
    title_options: list[str]
    caption_options: list[str]
    option_notes: list[str]
    option_tiers: list[str]
    recommended_title_index: int | None
    recommended_caption_index: int | None
    recommendation_reason: str | None
    title_style_preset: str | None
    caption_style_preset: str | None
    provider_label: str | None
    generation_meta: dict | list | str | None
    vision_payload: dict | list | str | None
    applied_at: str | None
    applied_title_index: int | None
    applied_caption_index: int | None

    def to_dict(self) -> dict:
        return asdict(self)


def _dump_list(values: list[str] | None) -> str | None:
    if not values:
        return None
    return json.dumps([str(v) for v in values], ensure_ascii=False)


def _load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _coerce_json_text(value: object) -> str | None:
    """Normalize a JSON-ish input to a stored JSON string (or ``None``).

    Accepts an already-serialized string, a dict/list, or ``None`` so both the
    CLI (which receives parsed JSON) and internal copies (which carry strings)
    can pass values through unchanged.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return json.dumps(value, ensure_ascii=False)


def _load_json_value(raw: str | None) -> dict | list | str | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _to_dto(row: DraftRevision) -> DraftRevisionDTO:
    return DraftRevisionDTO(
        id=row.id,
        download_item_id=row.download_item_id,
        revision_number=row.revision_number,
        source=row.source,
        created_at=_iso(row.created_at),
        summary=row.summary,
        title_options=_load_list(row.title_options),
        caption_options=_load_list(row.caption_options),
        option_notes=_load_list(row.option_notes),
        option_tiers=_load_list(row.option_tiers),
        recommended_title_index=row.recommended_title_index,
        recommended_caption_index=row.recommended_caption_index,
        recommendation_reason=row.recommendation_reason,
        title_style_preset=row.title_style_preset,
        caption_style_preset=row.caption_style_preset,
        provider_label=row.provider_label,
        generation_meta=_load_json_value(row.generation_meta),
        vision_payload=_load_json_value(row.vision_payload),
        applied_at=_iso(row.applied_at),
        applied_title_index=row.applied_title_index,
        applied_caption_index=row.applied_caption_index,
    )


def _require_item(session, item_id: int) -> DownloadItem:
    item = session.get(DownloadItem, item_id)
    if item is None:
        raise DraftRevisionError(f"No download item with id {item_id}.")
    return item


def _next_revision_number(session, item_id: int) -> int:
    rows = session.scalars(
        select(DraftRevision.revision_number).where(DraftRevision.download_item_id == item_id)
    ).all()
    return (max(rows) + 1) if rows else 1


def save_revision(
    item_id: int,
    *,
    title_options: list[str],
    caption_options: list[str],
    summary: str | None = None,
    option_notes: list[str] | None = None,
    option_tiers: list[str] | None = None,
    recommended_title_index: int | None = None,
    recommended_caption_index: int | None = None,
    recommendation_reason: str | None = None,
    title_style_preset: str | None = None,
    caption_style_preset: str | None = None,
    provider_label: str | None = None,
    generation_meta: object = None,
    vision_payload: object = None,
    source: str = "codex",
) -> DraftRevisionDTO:
    """Insert a new versioned revision for ``item_id`` and return it.

    ``title_options`` and ``caption_options`` are required and must be non-empty
    lists of strings. Other fields are optional. The new row gets the next
    ``revision_number`` for the item.
    """
    titles = [str(t).strip() for t in (title_options or []) if str(t).strip()]
    captions = [str(c) for c in (caption_options or []) if str(c).strip()]
    if not titles:
        raise DraftRevisionError("title_options must contain at least one non-empty title.")
    if not captions:
        raise DraftRevisionError("caption_options must contain at least one non-empty caption.")

    with get_session() as session:
        _require_item(session, item_id)
        revision = DraftRevision(
            download_item_id=item_id,
            revision_number=_next_revision_number(session, item_id),
            source=(source or "codex").strip() or "codex",
            summary=(summary or None),
            title_options=_dump_list(titles),
            caption_options=_dump_list(captions),
            option_notes=_dump_list(option_notes),
            option_tiers=_dump_list(option_tiers),
            recommended_title_index=recommended_title_index,
            recommended_caption_index=recommended_caption_index,
            recommendation_reason=(recommendation_reason or None),
            title_style_preset=(title_style_preset or None),
            caption_style_preset=(caption_style_preset or None),
            provider_label=(provider_label or None),
            generation_meta=_coerce_json_text(generation_meta),
            vision_payload=_coerce_json_text(vision_payload),
        )
        session.add(revision)
        session.commit()
        session.refresh(revision)
        return _to_dto(revision)


def list_revisions(item_id: int) -> list[DraftRevisionDTO]:
    """Return every revision for an item, oldest first."""
    with get_session() as session:
        rows = session.scalars(
            select(DraftRevision)
            .where(DraftRevision.download_item_id == item_id)
            .order_by(DraftRevision.revision_number.asc())
        ).all()
        return [_to_dto(row) for row in rows]


def latest_revision(item_id: int) -> DraftRevisionDTO | None:
    """Return the highest-numbered revision for an item, or ``None``."""
    with get_session() as session:
        row = session.scalars(
            select(DraftRevision)
            .where(DraftRevision.download_item_id == item_id)
            .order_by(DraftRevision.revision_number.desc())
            .limit(1)
        ).first()
        return _to_dto(row) if row is not None else None


def get_revision(revision_id: int) -> DraftRevisionDTO | None:
    with get_session() as session:
        row = session.get(DraftRevision, revision_id)
        return _to_dto(row) if row is not None else None


def _base_revision(item_id: int, revision_id: int | None) -> DraftRevisionDTO:
    if revision_id is not None:
        base = get_revision(revision_id)
        if base is None:
            raise DraftRevisionError(f"No draft revision with id {revision_id}.")
        if base.download_item_id != item_id:
            raise DraftRevisionError(f"Revision {revision_id} does not belong to item {item_id}.")
        return base
    base = latest_revision(item_id)
    if base is None:
        raise DraftRevisionError(
            f"Item {item_id} has no draft revisions yet; save one before revising."
        )
    return base


def revise_option(
    item_id: int,
    option_number: int,
    *,
    title: str | None = None,
    caption: str | None = None,
    note: str | None = None,
    revision_id: int | None = None,
    source: str = "codex",
) -> DraftRevisionDTO:
    """Create a new revision based on an existing one with a single option
    replaced.

    ``option_number`` is 1-based. At least one of ``title`` / ``caption`` /
    ``note`` must be provided. The base revision is ``revision_id`` when given,
    otherwise the latest revision for the item.
    """
    if title is None and caption is None and note is None:
        raise DraftRevisionError("Provide at least one of title, caption, or note to revise.")

    base = _base_revision(item_id, revision_id)
    count = max(len(base.title_options), len(base.caption_options))
    if not 1 <= option_number <= count:
        raise DraftRevisionError(
            f"option must be between 1 and {count} for this revision (got {option_number})."
        )

    index = option_number - 1
    titles = list(base.title_options)
    captions = list(base.caption_options)
    notes = list(base.option_notes)
    # Pad so the targeted index is always assignable even if the base revision
    # had uneven option lists.
    while len(titles) <= index:
        titles.append("")
    while len(captions) <= index:
        captions.append("")
    while len(notes) <= index:
        notes.append("")

    if title is not None:
        titles[index] = title.strip()
    if caption is not None:
        captions[index] = caption
    if note is not None:
        notes[index] = note.strip()

    return save_revision(
        item_id,
        title_options=[t for t in titles if t.strip()],
        caption_options=[c for c in captions if c.strip()],
        summary=base.summary,
        option_notes=[n for n in notes if n.strip()] or None,
        option_tiers=base.option_tiers or None,
        recommended_title_index=base.recommended_title_index,
        recommended_caption_index=base.recommended_caption_index,
        recommendation_reason=base.recommendation_reason,
        title_style_preset=base.title_style_preset,
        caption_style_preset=base.caption_style_preset,
        provider_label=base.provider_label,
        generation_meta=base.generation_meta,
        vision_payload=base.vision_payload,
        source=source,
    )


def apply_revision(
    item_id: int,
    option_number: int,
    *,
    revision_id: int | None = None,
) -> dict:
    """Apply one option from a revision to the item's live final draft.

    Copies the chosen title/caption onto ``DownloadItem.title_draft`` /
    ``caption_draft`` and mirrors the revision's options/metadata onto the
    ``smart_*`` fields so the existing export/publish UI shows the same state.
    Returns a small summary dict. ``option_number`` is 1-based.
    """
    base = _base_revision(item_id, revision_id)
    if not 1 <= option_number <= len(base.title_options):
        raise DraftRevisionError(
            f"option must be between 1 and {len(base.title_options)} " f"(got {option_number})."
        )
    index = option_number - 1
    chosen_title = base.title_options[index]
    chosen_caption = base.caption_options[index] if index < len(base.caption_options) else ""

    with get_session() as session:
        item = _require_item(session, item_id)
        revision = session.get(DraftRevision, base.id)
        if revision is None:  # pragma: no cover - base came from the same DB
            raise DraftRevisionError(f"No draft revision with id {base.id}.")

        item.title_draft = chosen_title or None
        item.caption_draft = chosen_caption or None
        # Mirror the option set + metadata so the existing Processing UI and
        # export path read the same content they would after a Groq run.
        item.smart_summary = base.summary or None
        item.smart_title_options = _dump_list(base.title_options)
        item.smart_caption_options = _dump_list(base.caption_options)
        item.smart_provider_label = base.provider_label or None
        item.smart_generation_meta = _coerce_json_text(base.generation_meta)
        item.smart_vision_payload = _coerce_json_text(base.vision_payload)
        item.smart_generated_at = revision.created_at
        if base.title_style_preset:
            item.title_style_preset = base.title_style_preset
        if base.caption_style_preset:
            item.caption_style_preset = base.caption_style_preset

        revision.applied_at = dt.datetime.now(dt.timezone.utc)
        revision.applied_title_index = option_number
        revision.applied_caption_index = option_number
        session.commit()

    return {
        "item_id": item_id,
        "revision_id": base.id,
        "revision_number": base.revision_number,
        "applied_option": option_number,
        "title_draft": chosen_title,
        "caption_draft": chosen_caption,
    }


def _account_voice(account: Account | None) -> dict | None:
    if account is None:
        return None
    return {
        "id": account.id,
        "name": account.name,
        "platform": account.platform,
        "niche_label": account.niche_label,
        "niche": account.niche,
        "writing_tone": account.writing_tone,
        "target_audience": account.target_audience,
        "hook_style": account.hook_style,
        "banned_phrases": account.banned_phrases,
        "title_style_notes": account.title_style_notes,
        "caption_style_notes": account.caption_style_notes,
    }


def resolve_active_item_id(explicit_item_id: int | None = None) -> int | None:
    """Resolve which item ``current``/``context`` should target.

    Order: an explicit id, then the ``active_processing_item_id`` UI pref when it
    points at a real row, then the most recently created download item that has a
    local file on disk (a real, processable clip).
    """
    if explicit_item_id is not None:
        return explicit_item_id

    with get_session() as session:
        pref = get_ui_pref(ACTIVE_PROCESSING_ITEM_PREF_KEY, None)
        if isinstance(pref, int) and session.get(DownloadItem, pref) is not None:
            return pref

        row = session.scalars(
            select(DownloadItem.id)
            .where(DownloadItem.file_path.is_not(None))
            .order_by(DownloadItem.id.desc())
            .limit(1)
        ).first()
        return row


def active_context(item_id: int | None = None) -> dict:
    """Return the Processing context Codex needs to generate drafts for an item.

    Includes item fields, the account "voice" config, the current saved final
    draft, and a compact view of the latest revision. Raises
    :class:`DraftRevisionError` when no item can be resolved.
    """
    resolved = resolve_active_item_id(item_id)
    if resolved is None:
        raise DraftRevisionError("No active item: pass --item-id, or download a clip first.")

    with get_session() as session:
        item = _require_item(session, resolved)
        account = session.get(Account, item.account_id) if item.account_id else None
        transcript = item.transcript_text or ""
        transcript_truncated = len(transcript) > _CONTEXT_TRANSCRIPT_CHARS
        context = {
            "item": {
                "id": item.id,
                "source_url": item.source_url,
                "title": item.title,
                "source_description": item.source_description,
                "file_path": item.file_path,
                "processed_path": item.processed_path,
                "status": item.status,
                "review_state": item.review_state,
                "transcript_text": transcript[:_CONTEXT_TRANSCRIPT_CHARS],
                "transcript_truncated": transcript_truncated,
                "title_draft": item.title_draft,
                "caption_draft": item.caption_draft,
                "title_style_preset": item.title_style_preset,
                "caption_style_preset": item.caption_style_preset,
            },
            "account": _account_voice(account),
        }

    latest = latest_revision(resolved)
    context["latest_revision"] = latest.to_dict() if latest is not None else None
    context["revision_count"] = len(list_revisions(resolved))
    return context
