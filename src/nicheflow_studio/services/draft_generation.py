"""Generate draft options for a Processing item and save them as a revision.

This is the UI-independent extraction of the PyQt "Generate smart drafts" flow:
it gathers the item + account context from the database, calls
``smart_drafts.generate_smart_drafts``, and persists the result as a versioned
:class:`DraftRevision` via ``draft_revisions.save_revision``.

It is the first real background-job payload (run through
``services.jobs.JobManager``) so the slow LLM call never blocks the bridge.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select

from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.post_metrics import top_titles_for_account
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import draft_guard, smart_drafts
from nicheflow_studio.services import draft_revisions, library
from nicheflow_studio.services.draft_revisions import DraftRevisionDTO, DraftRevisionError

logger = logging.getLogger(__name__)

# Number of recent same-account drafts fed to the generator so it can avoid
# repeating hooks/templates (mirrors the PyQt recent-drafts behavior).
_RECENT_DRAFTS_LIMIT = 8


def can_generate() -> bool:
    """True when a draft provider (Groq/Ollama) is configured."""
    return smart_drafts.can_generate_smart_drafts()


def guard_clip_titles(
    *,
    titles: list[str],
    grounding_text: str,
    source_title: str | None = None,
    niche_label: str | None = None,
    recommended_index: int | None = None,
    recommendation_reason: str | None = None,
    provider_label: str = "Pasted",
) -> dict:
    """Score already-written titles against the clip, in the API path's shape.

    Titles pasted back from a chat model get the same grounding check the Groq
    ones do — the check is about whether the clip supports the claim, not about
    who wrote it — so both routes hand the UI the same green/amber/red verdicts.
    """
    cleaned = [title.strip() for title in titles if title and title.strip()]
    if not cleaned:
        raise DraftRevisionError(
            "No 'Title Option' lines found. The reply must use plain-text headers "
            "(Title Option 1:) — a bolded or prefixed header breaks the parser."
        )
    guarded = draft_guard.guard_options(
        title_options=cleaned,
        signals_text=draft_guard.build_signals_text(
            grounding_text, source_title, niche_label, None
        ),
        recommended_index=recommended_index,
        recommendation_reason=recommendation_reason,
    )
    return {
        "titles": cleaned,
        "tiers": list(guarded.option_tiers),
        "notes": list(guarded.option_notes or []),
        "flagged_terms": {
            str(index): terms for index, terms in guarded.flagged_terms.items()
        },
        "recommended_index": guarded.recommended_index,
        "recommendation_shifted": bool(guarded.recommendation_shifted),
        "reason": guarded.recommendation_reason,
        "summary": "",
        "provider_label": provider_label,
        "used_fallback": False,
    }


def generate_titles_for_clip(
    *,
    account_id: int,
    transcript_text: str,
    video_path: str | None = None,
    source_title: str | None = None,
    source_description: str | None = None,
    grounding_text: str | None = None,
    title_style: str | None = None,
    title_length: str | None = None,
) -> dict:
    """Title options for a Clip Studio candidate, before any library item exists.

    Clip Studio ranks moments and renders them, but had nothing to put in the
    title band: the hook was typed by hand, and a candidate rendered without one
    is exactly as hard to judge as the raw cut. Meanwhile the same generator has
    been writing titles for Processing all along, and a candidate already carries
    the one thing it needs — the words spoken inside the window.

    Deliberately not :func:`generate_revision_for_item`: that one is keyed on a
    ``DownloadItem`` and persists a revision, and a candidate is neither. This
    borrows only the account's voice, so a suggested title lands in the same
    register the account actually publishes in, and returns options rather than
    saving anything — most candidates in a batch are never sent anywhere.

    ``video_path`` is the cut preview: passing it lets the generator ground on
    the frames as well as the words, which is what stops a title describing
    something the clip never shows.

    ``grounding_text`` is what the result is *checked* against, and is
    deliberately allowed to differ from what it was written from. The clip window
    alone is too thin to write a good title from, but it is exactly the right
    thing to verify one against: the title has to be delivered by the fifteen
    seconds a viewer actually sees. So the writer reads the surrounding minute
    and the guard reads only the clip — a title that drifts onto something the
    clip never shows ("a secret global handoff") comes back flagged rather than
    recommended.
    """
    cleaned_transcript = (transcript_text or "").strip()
    with get_session() as session:
        account = session.get(Account, int(account_id))
        if account is None:
            raise DraftRevisionError(f"No account with id {account_id}.")
        niche_label = account.niche_label
        voice = _account_voice(account, None)
        account_key = account.instagram_handle
        # The account's saved prompt style, the same fields processing_workflow's
        # get_settings reads for an item. Without them a history account's clip
        # came back in a flat reporting register ("Around $60 million of fraud may
        # have occurred") instead of the one it actually publishes in — the style
        # is the whole difference between a usable title and a caption.
        try:
            prefs = json.loads(account.processing_preferences or "{}")
        except ValueError:
            prefs = {}
        if not isinstance(prefs, dict):
            prefs = {}
        title_style = title_style or prefs.get("prompt_title_style") or None
        title_length = title_length or prefs.get("title_length") or None
        has_context = bool(cleaned_transcript or niche_label or voice)
    if not has_context:
        raise DraftRevisionError(
            "Nothing to write a title from — this clip has no transcribed speech "
            "and the account has no niche or voice set."
        )

    clip_path = Path(video_path) if video_path else None
    drafts = smart_drafts.generate_smart_drafts(
        transcript_text=cleaned_transcript,
        source_title=source_title,
        source_description=source_description,
        niche_label=niche_label,
        input_path=clip_path if clip_path and clip_path.exists() else None,
        account_voice=voice,
        title_style=title_style,
        title_length=title_length,
        few_shot_winners=(top_titles_for_account(account_key) or None) if account_key else None,
        # A clip with speech is already grounded in its own words, and a hard
        # vision requirement would fail the whole suggestion on a source whose
        # frames cannot be sampled — worse than a slightly weaker title.
        require_vision=False,
    )
    # Checked against the clip alone, whatever it was written from.
    guarded = draft_guard.guard_options(
        title_options=drafts.title_options,
        signals_text=draft_guard.build_signals_text(
            (grounding_text or cleaned_transcript),
            source_title,
            niche_label,
            drafts.vision_payload,
        ),
        option_tiers=drafts.option_tiers,
        option_notes=drafts.option_notes,
        claim_supports=drafts.claim_supports,
        recommended_index=drafts.recommended_title_index,
        recommendation_reason=drafts.recommendation_reason,
    )
    return {
        "titles": list(drafts.title_options),
        # "green" (no checkable claim), "yellow" (claims supported), "red"
        # (a claim the clip does not back up).
        "tiers": list(guarded.option_tiers),
        "notes": list(guarded.option_notes or []),
        "flagged_terms": {
            str(index): terms for index, terms in guarded.flagged_terms.items()
        },
        "recommended_index": guarded.recommended_index,
        "recommendation_shifted": bool(guarded.recommendation_shifted),
        "reason": guarded.recommendation_reason,
        "summary": drafts.summary,
        "provider_label": drafts.provider_label,
        "used_fallback": bool(drafts.used_fallback),
    }


# A provider-level vision failure (model_not_found, revoked key, no key at all)
# fails identically for every item in a batch, and each attempt costs a full
# round-trip. Recognising those lets a batch stop after the first one instead of
# paying the timeout N times. Rate limits are NOT in this set: those are
# per-key and transient, and run_vision_pass already rotates keys for them.
_VISION_UNAVAILABLE_MARKERS = ("model_not_found", "404", "invalid_api_key", "401", "403")


def _vision_unavailable(error: str | None) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in _VISION_UNAVAILABLE_MARKERS)


def _stored_vision_payload(item_id: int) -> str:
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        return (item.smart_vision_payload or "").strip() if item else ""


def vision_pass_for_item(item_id: int, *, force: bool = False) -> smart_drafts.VisionPass | None:
    """Run the visual-evidence pass for one item and persist the payload.

    Returns None when the item already had usable evidence (nothing to do) or
    has no video on disk; otherwise the :class:`VisionPass`, whose ``error``
    tells a batch caller whether retrying the next item is worth it.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        existing = (item.smart_vision_payload or "").strip()
        if existing and not force:
            return None  # already grounded; nothing to run
        account = session.get(Account, item.account_id) if item.account_id else None
        video_path = Path(item.file_path) if item.file_path else None
        context = {
            "transcript_text": item.transcript_text or "",
            "source_title": item.title,
            "source_description": item.source_description,
            "niche_label": account.niche_label if account is not None else None,
        }

    if video_path is None or not video_path.exists():
        return None

    # The vision call is slow and must run outside the DB session.
    result = smart_drafts.describe_video_frames(video_path, **context)
    if result.payload is not None:
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is not None:
                item.smart_vision_payload = json.dumps(result.payload, indent=2, sort_keys=True)
                session.commit()
    return result


def ensure_vision_payload(item_id: int, *, force: bool = False) -> dict | None:
    """The item's visual-evidence JSON, running the vision pass if needed.

    The batch chat handoff sends this JSON to the external assistant as TEXT
    instead of attaching a still frame per reel: the vision model reads up to 5
    sampled frames plus burned-in on-screen text, where an attachment is one
    still, at roughly a twentieth of the tokens.

    Returns None when there is no video on disk, no working Groq vision model,
    or the pass failed. Never raises for those cases: batch prep must still
    work from the source caption alone.
    """
    result = vision_pass_for_item(item_id, force=force)
    if result is not None:
        return result.payload
    # None means "nothing was run" — either already grounded, or no video.
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        stored = (item.smart_vision_payload or "").strip() if item else ""
    if not stored:
        return None
    try:
        return json.loads(stored)
    except ValueError:
        return None


def ensure_batch_vision_payloads(item_ids: list[int], *, force: bool = False) -> dict:
    """Fill in missing visual evidence for a batch, best effort.

    One failing item must not sink the batch prep, so per-item errors are
    collected and reported rather than raised. A provider-level failure (no
    vision model on the account, bad key) aborts the remaining items instead of
    paying an identical failed round-trip per reel.
    """
    described: list[int] = []
    skipped: list[dict] = []
    unavailable: str | None = None
    for item_id in item_ids:
        if unavailable is not None:
            skipped.append({"item_id": item_id, "reason": unavailable})
            continue
        try:
            result = vision_pass_for_item(item_id, force=force)
            if result is None:
                # Nothing ran: the item was already grounded, or has no video.
                if _stored_vision_payload(item_id):
                    described.append(item_id)
                else:
                    skipped.append({"item_id": item_id, "reason": "no video on disk"})
            elif result.payload is not None:
                described.append(item_id)
            else:
                reason = result.error or "vision returned no payload"
                if _vision_unavailable(result.error):
                    unavailable = reason  # every remaining item fails the same way
                skipped.append({"item_id": item_id, "reason": reason})
        except Exception as exc:  # noqa: BLE001 - vision is an optional enrichment
            logger.warning("Vision pass failed for item %s: %s", item_id, exc)
            skipped.append({"item_id": item_id, "reason": str(exc)})
    return {"described": described, "skipped": skipped, "unavailable": unavailable}


# Per-niche follow outro appended to every generated caption (before hashtags).
# The promise ("every day") is the reason to follow; the @handle is there
# because most Reels viewers never notice the corner username. Niches without
# an entry fall back to the generic line; accounts without a handle get none.
_CAPTION_OUTROS_BY_NICHE = {
    "history": "Lost moments from history, every day → @{handle}",
    "movie": "One unforgettable scene a day → @{handle}",
}
_CAPTION_OUTRO_DEFAULT = "More every day → @{handle}"

# Disabled across all accounts (2026-06-18): the user's analytics showed the
# follow outro had no measurable effect on follows or engagement, and a fixed
# signature on every caption reads as templated. The niche templates above are
# kept dormant so the line can be re-enabled later by flipping this flag.
_CAPTION_OUTRO_ENABLED = False


def caption_outro_for_account(account: Account | None) -> str | None:
    """The account's follow-outro caption line.

    Returns None for every account while the outro is disabled (see
    ``_CAPTION_OUTRO_ENABLED``). When re-enabled, returns the per-niche line for
    an account that has an Instagram handle, or None when the handle is missing.
    """
    if not _CAPTION_OUTRO_ENABLED or account is None:
        return None
    handle = (account.instagram_handle or "").strip().lstrip("@")
    if not handle:
        return None
    template = _CAPTION_OUTROS_BY_NICHE.get(
        (account.niche or "").strip().lower(), _CAPTION_OUTRO_DEFAULT
    )
    return template.format(handle=handle)


def _space_caption_outro(caption: str, outro: str | None) -> str:
    """Keep the follow outro readable even when the model ignores paragraph rules."""
    if not outro:
        return caption
    lines = caption.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        index = next(i for i, line in enumerate(lines) if line.strip() == outro)
    except StopIteration:
        return caption
    before = "\n".join(lines[:index]).rstrip()
    after = "\n".join(lines[index + 1 :]).lstrip()
    return "\n\n".join(part for part in (before, outro, after) if part)


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
    title_length: str | None = None,
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
            pending_item and pending_item.status == "pending_review" and not pending_item.file_path
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
            (item.title or "").strip() or (item.source_description or "").strip() or niche_context
        )
        if not (has_transcript or has_video or has_text_context):
            raise DraftRevisionError(
                "This item has nothing to generate from yet — no transcript, no video "
                "on disk, and no source caption. Add a transcript or caption first."
            )
        voice = _account_voice(account, clip_premise)
        recent_titles, recent_captions = _recent_drafts(session, item.account_id, item.id)
        account_key = account.instagram_handle if account is not None else None
        few_shot_winners = top_titles_for_account(account_key) if account_key else []
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
            "title_length": title_length,
            "recent_titles": recent_titles or None,
            "recent_captions": recent_captions or None,
            "few_shot_winners": few_shot_winners or None,
            "caption_outro": caption_outro_for_account(account),
            "require_vision": True,
        }

    # The provider call is slow and must run outside the DB session.
    drafts = smart_drafts.generate_smart_drafts(**gen_kwargs)
    caption_outro = gen_kwargs["caption_outro"]
    caption_options = [
        _space_caption_outro(caption, caption_outro) for caption in drafts.caption_options
    ]

    # Deterministic grounding check: the model self-rates option_tiers, but a
    # claim word with no signal support still slips through ("heavy" prams).
    # The guard downgrades such options to red and moves the recommendation
    # off them; title+caption indexes always shift together (the Apply button
    # applies one option number as a unit).
    account_voice = gen_kwargs["account_voice"] or {}
    guarded = draft_guard.guard_options(
        title_options=drafts.title_options,
        signals_text=draft_guard.build_signals_text(
            gen_kwargs["transcript_text"],
            gen_kwargs["source_title"],
            gen_kwargs["niche_label"],
            account_voice.get("clip_context"),
            drafts.vision_payload,
        ),
        asserted_signals_text=draft_guard.build_signals_text(
            gen_kwargs["source_description"],
        ),
        option_tiers=drafts.option_tiers,
        option_notes=drafts.option_notes,
        claim_supports=drafts.claim_supports,
        recommended_index=drafts.recommended_title_index,
        recommendation_reason=drafts.recommendation_reason,
    )
    if guarded.recommendation_shifted:
        recommended_title_index = recommended_caption_index = guarded.recommended_index
    else:
        recommended_title_index = drafts.recommended_title_index
        recommended_caption_index = drafts.recommended_caption_index
    generation_meta = drafts.generation_meta
    if guarded.flagged_terms or guarded.recommendation_shifted:
        generation_meta = dict(generation_meta or {})
        generation_meta["grounding_guard"] = {
            "flagged_options": {
                str(index + 1): terms for index, terms in sorted(guarded.flagged_terms.items())
            },
            "recommendation_shifted": guarded.recommendation_shifted,
        }

    return draft_revisions.save_revision(
        item_id,
        title_options=drafts.title_options,
        caption_options=caption_options,
        summary=drafts.summary,
        option_notes=guarded.option_notes,
        option_tiers=guarded.option_tiers,
        recommended_title_index=_one_based(recommended_title_index),
        recommended_caption_index=_one_based(recommended_caption_index),
        recommendation_reason=guarded.recommendation_reason,
        title_style_preset=title_style,
        caption_style_preset=caption_style,
        provider_label=drafts.provider_label,
        generation_meta=generation_meta,
        vision_payload=drafts.vision_payload,
        source=source,
    )
