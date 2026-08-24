"""Intake: turn a video file into a library item the rest of the app can see.

Everything downstream of acquisition — draft generation, export, distribution,
scheduling, publishing — hangs off a ``DownloadItem`` row. Clip Studio and a
manually downloaded file both produce a video on disk and nothing else, so
neither could reach any of it. This module is the single seam that closes that
gap, and both intake paths go through it:

* **Sourced (Clip Studio):** :func:`register_moment` cuts the chosen span out of
  the cached source and registers the raw cut. The words spoken in that window
  are stored as ``transcript_text``, so the draft prompt is grounded on what the
  clip actually says without the operator pasting anything.
* **Manual:** :func:`register_clip` takes a file the operator downloaded by hand
  (a campaign clip pack, say) and registers it directly.

Deliberately no rendering here. The clip lands in Processing as a normal item
and gets its title, crop and export from the same path every other reel uses,
so there is one export recipe in the app rather than two that can drift.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

from nicheflow_studio.core.media_tools import (
    ffmpeg_binary,
    ffprobe_binary,
    subprocess_run_kwargs,
)
from sqlalchemy import select

from nicheflow_studio.core.paths import downloads_dir
from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

# Containers ffmpeg can read and the pipeline can work with. `.mov` matters:
# campaign clip packs are shared as QuickTime, which the old PyQt importer
# rejected outright.
SUPPORTED_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm"})

# The only video codec the in-app <video> preview can be relied on to play.
# iPhone-shot .mov is frequently HEVC, which decodes in ffmpeg but shows up as a
# black player in the review screen — so anything else gets re-encoded on intake.
_PREVIEWABLE_VIDEO_CODEC = "h264"

# Two cuts this close are the same cut. The grid stores trims to one decimal and
# the ranker's own boundaries move by more than this between candidates, so the
# window is wide enough to catch a re-send and far too narrow to swallow a
# deliberate nudge.
_SAME_CUT_TOLERANCE_SECONDS = 0.25

# An item the operator already set aside is not a duplicate worth blocking on:
# rejecting a clip and cutting it again is a normal correction. Mirrors
# services.library._SKIPPED_REVIEW_STATES.
_SET_ASIDE_REVIEW_STATES = frozenset(
    {"ignored", "skipped", "declined", "canceled", "cancelled", "rejected", "blocked"}
)


class ClipIntakeError(ServiceError):
    """Raised when a file cannot be taken into the library.

    A ``ServiceError``, so the bridge shows the operator the actual reason
    ("that .txt is not a video", "pick an account first") instead of the generic
    unexpected-error message.
    """


def _existing_cut(
    *,
    clip_source_ref: str | None,
    account_id: int,
    start_seconds: float,
    end_seconds: float,
) -> int | None:
    """Item id of a live clip already cut from this window, if there is one.

    Returns ``None`` when the source is unknown: a clip with no
    ``clip_source_ref`` cannot be tied back to a window, so there is nothing to
    compare against and refusing would be guesswork.
    """
    source_ref = (clip_source_ref or "").strip()
    if not source_ref:
        return None
    with get_session() as session:
        rows = session.execute(
            select(
                DownloadItem.id,
                DownloadItem.clip_start_seconds,
                DownloadItem.clip_end_seconds,
                DownloadItem.review_state,
            )
            .where(DownloadItem.clip_source_ref == source_ref)
            .where(DownloadItem.account_id == account_id)
            .order_by(DownloadItem.id)
        ).all()
    for item_id, existing_start, existing_end, review_state in rows:
        if existing_start is None or existing_end is None:
            # Cut before the window was recorded. Unknowable, so not blocked.
            continue
        if (review_state or "").lower() in _SET_ASIDE_REVIEW_STATES:
            continue
        if (
            abs(existing_start - start_seconds) <= _SAME_CUT_TOLERANCE_SECONDS
            and abs(existing_end - end_seconds) <= _SAME_CUT_TOLERANCE_SECONDS
        ):
            return int(item_id)
    return None


def _safe_stem(path: Path) -> str:
    safe = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "_" for char in path.stem
    ).strip("._")
    return safe or "video"


def _destination_for(source_path: Path, *, prefix: str, suffix: str) -> Path:
    """A collision-free path under ``downloads_dir()/local`` for an intake copy."""
    import_dir = downloads_dir() / "local"
    import_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = _safe_stem(source_path)
    destination = import_dir / f"{prefix}_{timestamp}_{stem}{suffix}"
    counter = 1
    while destination.exists():
        destination = import_dir / f"{prefix}_{timestamp}_{stem}_{counter}{suffix}"
        counter += 1
    return destination


def _video_codec(file_path: Path) -> str | None:
    """The first video stream's codec name, or ``None`` when it can't be read."""
    ffprobe_path = ffprobe_binary()
    if ffprobe_path is None:
        return None
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, **subprocess_run_kwargs()
        )
        streams = json.loads(result.stdout).get("streams") or [{}]
        name = streams[0].get("codec_name")
        return str(name).lower() if name else None
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError):
        return None


def _normalize_to_mp4(source_path: Path, destination: Path) -> None:
    """Write ``source_path`` to ``destination`` as a preview-safe MP4.

    Remuxes without re-encoding when the video is already H.264, which is a
    second or two even on a long clip. Anything else (HEVC from a phone, VP9
    from a web download) is re-encoded, because it would otherwise import fine
    and then show a black player in review.
    """
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise ClipIntakeError("ffmpeg is not installed, so this file cannot be imported.")

    can_copy = _video_codec(source_path) == _PREVIEWABLE_VIDEO_CODEC
    video_args = ["-c:v", "copy"] if can_copy else [
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
    ]
    command = [
        str(ffmpeg_path), "-y", "-i", str(source_path),
        *video_args,
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(destination),
    ]
    try:
        subprocess.run(
            command, check=True, capture_output=True, text=True, **subprocess_run_kwargs()
        )
    except subprocess.CalledProcessError as error:
        raise ClipIntakeError(
            f"Could not convert {source_path.name} to MP4: {(error.stderr or '').strip()[-400:]}"
        ) from error


def _source_reference(source_url: str | None, destination: Path) -> str:
    """A stable ``source_url`` for the row; real URLs win, else a local marker."""
    reference = (source_url or "").strip()
    if reference.lower().startswith(("http://", "https://")):
        return reference
    return f"local://{destination.name}"


def register_clip(
    video_path: Path | str,
    *,
    account_id: int,
    title: str | None = None,
    source_url: str | None = None,
    transcript_context: str | None = None,
    caption_draft: str | None = None,
    clip_source_ref: str | None = None,
    clip_window: tuple[float, float] | None = None,
    extractor: str = "local",
) -> dict:
    """Copy ``video_path`` into the library and create its ``DownloadItem``.

    The file is copied rather than referenced, so the library keeps working when
    the operator clears their Downloads folder. Non-MP4 input is normalized on
    the way in (see :func:`_normalize_to_mp4`).
    """
    source_path = Path(video_path).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise ClipIntakeError(f"No such video file: {source_path}")
    suffix = source_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ClipIntakeError(
            f"{suffix or 'That file'} is not a supported video "
            f"({', '.join(sorted(SUPPORTED_SUFFIXES))})."
        )

    with get_session() as session:
        if session.get(Account, account_id) is None:
            raise ClipIntakeError(f"No account with id {account_id}.")

    destination = _destination_for(source_path, prefix="clip", suffix=".mp4")
    if suffix == ".mp4" and _video_codec(source_path) == _PREVIEWABLE_VIDEO_CODEC:
        shutil.copy2(source_path, destination)
    else:
        _normalize_to_mp4(source_path, destination)

    resolved_title = (title or "").strip() or source_path.stem
    stored_url = _source_reference(source_url, destination)
    cleaned_context = (transcript_context or "").strip() or None

    with get_session() as session:
        item = DownloadItem(
            source_url=stored_url,
            extractor=extractor,
            video_id=destination.stem,
            title=resolved_title,
            file_path=str(destination),
            transcript_text=cleaned_context,
            # A campaign caption is already composed and rule-checked before the
            # clip is sent over, so it arrives in Processing filled in rather
            # than being retyped there.
            caption_draft=(caption_draft or "").strip() or None,
            # Which long source this was cut from, so Clip Studio's history can
            # list the clips a source produced (services/clip_history.py).
            clip_source_ref=(clip_source_ref or "").strip() or None,
            clip_start_seconds=clip_window[0] if clip_window else None,
            clip_end_seconds=clip_window[1] if clip_window else None,
            account_id=account_id,
            status="downloaded",
            review_state="new",
        )
        session.add(item)
        session.flush()
        # Read the values out before committing; expire_on_commit would other-
        # wise make each attribute below a fresh round trip.
        record = {
            "item_id": item.id,
            "title": item.title,
            "file_path": item.file_path,
            "account_id": item.account_id,
            "source_url": item.source_url,
            "clip_source_ref": item.clip_source_ref,
            "has_transcript_context": cleaned_context is not None,
        }
        session.commit()
        return record


def register_moment(
    source_path: Path | str,
    *,
    start_seconds: float,
    end_seconds: float,
    account_id: int,
    title: str | None = None,
    source_url: str | None = None,
    transcript_context: str | None = None,
    caption_draft: str | None = None,
    clip_source_ref: str | None = None,
    transcript_path: Path | str | None = None,
    burn_captions: bool = False,
    force: bool = False,
) -> dict:
    """Cut ``[start, end)`` out of a Clip Studio source and register the cut.

    The cut is raw — no crop, no title, no template. Those are Processing's job,
    so a sourced clip and a manually imported one reach export by the same route.

    ``burn_captions`` is the exception: subtitles are burned in here because they
    belong to the footage and because Processing never sees the source
    transcript. Without this, a foreign-language clip reaches export with no way
    left to caption it.

    Sending the same window of the same source to the same account twice is
    refused unless ``force``. A batch offers eight candidates off one source and
    the grid does not survive a reload, so re-sending one already sent is an easy
    mistake to make, and it ends as the same clip published twice from one
    account. Keyed per account on purpose: the same moment on two *different*
    accounts is a deliberate call, not a slip.
    """
    from nicheflow_studio.services import clip_studio

    resolved_source = Path(source_path).expanduser().resolve()
    if not resolved_source.exists():
        raise ClipIntakeError(f"No such source video: {resolved_source}")
    if end_seconds <= start_seconds:
        raise ClipIntakeError(
            f"End ({end_seconds}) must be after start ({start_seconds})."
        )
    if not force:
        duplicate = _existing_cut(
            clip_source_ref=clip_source_ref,
            account_id=account_id,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        if duplicate is not None:
            raise ClipIntakeError(
                f"This exact window was already sent to this account as item "
                f"#{duplicate}. Reject that item first, or nudge the trim if you "
                f"meant a different cut."
            )

    cut_dir = downloads_dir() / "local"
    cut_dir.mkdir(parents=True, exist_ok=True)
    staged = _destination_for(
        resolved_source, prefix=f"moment_{int(start_seconds)}", suffix=".mp4"
    )
    clip_studio.cut_moment(
        resolved_source,
        staged,
        start_seconds,
        end_seconds,
        transcript_path=Path(transcript_path) if transcript_path else None,
        burn_captions=burn_captions,
    )
    try:
        return register_clip(
            staged,
            account_id=account_id,
            title=title,
            source_url=source_url,
            transcript_context=transcript_context,
            caption_draft=caption_draft,
            clip_source_ref=clip_source_ref,
            clip_window=(float(start_seconds), float(end_seconds)),
            extractor="clip_studio",
        )
    finally:
        # register_clip copied it into place under its own name; the staging cut
        # would otherwise sit in the same folder as a confusing duplicate.
        staged.unlink(missing_ok=True)
