"""Generate draft options for a Processing item and save them as a revision.

This is the UI-independent extraction of the PyQt "Generate smart drafts" flow:
it gathers the item + account context from the database, calls
``smart_drafts.generate_smart_drafts``, and persists the result as a versioned
:class:`DraftRevision` via ``draft_revisions.save_revision``.

It is the first real background-job payload (run through
``services.jobs.JobManager``) so the slow LLM call never blocks the bridge.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import smart_drafts
from nicheflow_studio.services import draft_revisions, publishing_dashboard, library
from nicheflow_studio.services.draft_revisions import DraftRevisionDTO, DraftRevisionError

# Number of recent same-account drafts fed to the generator so it can avoid
# repeating hooks/templates (mirrors the PyQt recent-drafts behavior).
_RECENT_DRAFTS_LIMIT = 8


def can_generate() -> bool:
    """True when a draft provider (Groq/Ollama) is configured."""
    return smart_drafts.can_generate_smart_drafts()


def _account_voice(account: Account | None, clip_premise: str | None) -> dict[str, str] | None:
    """Build the account "voice" config from Account columns (UI-free mirror of
    MainWindow._account_voice_config) plus an optional clip premise."""
    voice: dict[str, str] = {}
    if account is not None:
        candidates = {
            "tone": account.writing_tone or "",
            "target_audience": account.target_audience or "",
            "hook_style": account.hook_style or "",
            "banned_phrases": account.banned_phrases or "",
            "title_style": account.title_style_notes or "",
            "caption_style": account.caption_style_notes or "",
        }
        voice = {key: value for key, value in candidates.items() if value.strip()}
    if clip_premise and clip_premise.strip():
        voice["clip_context"] = clip_premise.strip()
    return voice or None


def _recent_drafts(
    session, account_id: int | None, exclude_item_id: int
) -> tuple[list[str], list[str]]:
    if account_id is None:
        return [], []
    rows = session.execute(
        select(DownloadItem.title_draft, DownloadItem.caption_draft)
        .where(DownloadItem.account_id == account_id)
        .where(DownloadItem.id != exclude_item_id)
        .where((DownloadItem.title_draft.is_not(None)) | (DownloadItem.caption_draft.is_not(None)))
        .order_by(DownloadItem.smart_generated_at.desc())
        .limit(_RECENT_DRAFTS_LIMIT)
    ).all()
    titles = [row[0] for row in rows if row[0]]
    captions = [row[1] for row in rows if row[1]]
    return titles, captions


def _one_based(index: int | None) -> int | None:
    """SmartDrafts uses 0-based recommended indexes; DraftRevision uses 1-based."""
    return None if index is None else index + 1


def generate_revision_for_item(
    item_id: int,
    *,
    caption_style: str | None = None,
    title_style: str | None = None,
    prompt_profile: str | None = None,
    clip_premise: str | None = None,
    source: str = "ui",
) -> DraftRevisionDTO:
    """Generate options for an item and save them as a new draft revision.

    Raises :class:`DraftRevisionError` for a missing item, or when the item has
    nothing to ground on at all (no transcript, no video on disk, no
    caption/niche). A missing transcript alone is fine — the generator falls back
    to vision over the video frames plus the source caption (smart_drafts'
    "No-transcript mode"), which is the normal path for speechless reels.
    """
    with get_session() as session:
        pending_item = session.get(DownloadItem, item_id)
        should_download = bool(
            pending_item
            and pending_item.status == "pending_review"
            and not pending_item.file_path
        )
    if should_download:
        library.ensure_item_downloaded(item_id)
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        account = session.get(Account, item.account_id) if item.account_id else None
        # Don't hard-require a transcript: most scraped reels have no speech, and
        # the generator grounds drafts on the video frames (vision) + source
        # caption + niche in that case (smart_drafts' "No-transcript mode").
        # Refuse only when there is nothing at all to anchor to.
        has_transcript = bool(item.transcript_text and item.transcript_text.strip())
        has_video = bool(item.file_path and Path(item.file_path).exists())
        niche_context = (account.niche_label or account.niche) if account is not None else None
        has_text_context = bool(
            (item.title or "").strip()
            or (item.source_description or "").strip()
            or niche_context
        )
        if not (has_transcript or has_video or has_text_context):
            raise DraftRevisionError(
                "This item has nothing to generate from yet — no transcript, no video "
                "on disk, and no source caption. Add a transcript or caption first."
            )
        voice = _account_voice(account, clip_premise)
        recent_titles, recent_captions = _recent_drafts(session, item.account_id, item.id)
        few_shot_winners = (
            publishing_dashboard.top_post_titles(account.id) if account is not None else []
        )
        gen_kwargs = {
            "transcript_text": item.transcript_text or "",
            "source_title": item.title,
            "source_description": item.source_description,
            "niche_label": account.niche_label if account is not None else None,
            "input_path": Path(item.file_path) if item.file_path else None,
            "account_voice": voice,
            "prompt_profile": prompt_profile,
            "caption_style": caption_style,
            "title_style": title_style,
            "recent_titles": recent_titles or None,
            "recent_captions": recent_captions or None,
            "few_shot_winners": few_shot_winners or None,
        }

    # The provider call is slow and must run outside the DB session.
    drafts = smart_drafts.generate_smart_drafts(**gen_kwargs)

    return draft_revisions.save_revision(
        item_id,
        title_options=drafts.title_options,
        caption_options=drafts.caption_options,
        summary=drafts.summary,
        option_notes=drafts.option_notes,
        option_tiers=drafts.option_tiers,
        recommended_title_index=_one_based(drafts.recommended_title_index),
        recommended_caption_index=_one_based(drafts.recommended_caption_index),
        recommendation_reason=drafts.recommendation_reason,
        title_style_preset=title_style,
        caption_style_preset=caption_style,
        provider_label=drafts.provider_label,
        generation_meta=drafts.generation_meta,
        vision_payload=drafts.vision_payload,
        source=source,
    )
