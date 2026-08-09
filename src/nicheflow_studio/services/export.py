"""Render a processed Reel for a Processing item (UI-independent).

Extraction of the core PyQt "Export" path: auto-detect the crop, burn the
applied title onto the clip with ``processing.video.export_cropped_video``, and
record the output on ``DownloadItem.processed_path``. Designed to run as a
background job (``services.jobs.JobManager``) with coarse progress reporting so
the slow FFmpeg render never blocks the bridge.

v1 uses the default black-canvas (``top_band``) title style. Per-account style
controls (font, color, layout, audio alteration, watermark) from the PyQt
template system are a documented follow-up; ``Account.processing_preferences``
will feed them when the React style controls land.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

from nicheflow_studio.core.paths import account_headers_dir, processed_dir
from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import video
from nicheflow_studio.processing.post_header import PostHeader
from nicheflow_studio.processing.watermark import replace_detected_watermark
from nicheflow_studio.services import publishing, library
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.services.jobs import JobCanceled
from nicheflow_studio.services.processing_workflow import render_config

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]


def _check_cancel(cancel_event: "threading.Event | None") -> None:
    """Abort the export with :class:`JobCanceled` if cancellation was requested.

    Called at the cheap gaps between the expensive stages (download, render,
    watermark, auto-schedule) so a cancel that lands between them stops the job
    before the next stage starts. The render stage itself is interrupted by
    passing the same event down to :func:`video.export_cropped_video`.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise JobCanceled("Export canceled.")


class ExportError(ServiceError):
    """Raised when an item cannot be exported (missing file, ffmpeg, etc.)."""


# --- Per-item manual crop override ---------------------------------------- #
#
# A crop override is a normalized keep-region {x, y, w, h} in [0, 1] (fractions of
# the source width/height). When an item has one, export uses it instead of the
# auto-detected crop, so a bad auto-crop can be fixed for a single clip without
# touching ``suggest_title_replacement_crop`` (which would affect every video).


def _valid_rect(rect: dict) -> bool:
    try:
        x, y, w, h = (float(rect[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        0.0 <= x < 1.0
        and 0.0 <= y < 1.0
        and 0.0 < w <= 1.0
        and 0.0 < h <= 1.0
        and x + w <= 1.0001
        and y + h <= 1.0001
    )


def _parse_crop_override(raw: str | None) -> dict | None:
    """Parsed, validated keep-region from the stored JSON, or ``None``."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not _valid_rect(data):
        return None
    return {k: float(data[k]) for k in ("x", "y", "w", "h")}


# --- Burned-in post header (avatar + name + verified badge) ---------------- #
#
# Identity for the header comes from the publishing account, not the template:
# the avatar is a local file in ``data/account_headers/`` named after the
# account's @handle, and the display name / badge come from the account's
# processing preferences. Nothing is fetched from Instagram — see the
# account-safety rule about never authenticating as a publishing account.

_HEADER_AVATAR_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def _account_preferences(account: Account) -> dict:
    if not account.processing_preferences:
        return {}
    try:
        value = json.loads(account.processing_preferences)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _is_avatar_variant(stem: str, handle: str) -> bool:
    """True for ``handle`` plus a version suffix: ``pastmomentsdaily_2``, ``…_pp4``.

    The character after the handle must not be a letter, so ``beneathhistory``
    never claims a ``beneathhistoryclub`` file.
    """
    if not stem.startswith(handle):
        return False
    rest = stem[len(handle) :]
    return not rest or not rest[0].isalpha()


def _header_avatar_path(account: Account, prefs: dict) -> Path | None:
    configured = str(prefs.get("header_avatar_path") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    handle = (account.instagram_handle or account.name or "").strip().lstrip("@").lower()
    if not handle:
        return None
    headers_dir = account_headers_dir()
    exact = [headers_dir / f"{handle}{suffix}" for suffix in _HEADER_AVATAR_SUFFIXES]
    for candidate in exact:
        if candidate.is_file():
            return candidate
    if not headers_dir.is_dir():
        return None
    # Fall back to the newest versioned variant so swapping an account's
    # profile picture is a file drop, not a rename.
    variants = [
        path
        for path in headers_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in _HEADER_AVATAR_SUFFIXES
        and _is_avatar_variant(path.stem.lower(), handle)
    ]
    if not variants:
        return None
    return max(variants, key=lambda path: path.stat().st_mtime)


def build_post_header(account: Account | None) -> PostHeader | None:
    """The header spec for ``account``, or ``None`` when it has no display name.

    Must be called inside the session that loaded ``account`` — it reads lazy
    ORM attributes.
    """
    if account is None:
        return None
    prefs = _account_preferences(account)
    display_name = str(prefs.get("header_display_name") or "").strip() or (
        account.name or ""
    ).strip()
    if not display_name:
        return None
    return PostHeader(
        display_name=display_name,
        avatar_path=_header_avatar_path(account, prefs),
        verified=bool(prefs.get("header_verified", True)),
    )


def _coerce_rect(rect: dict) -> dict:
    if not isinstance(rect, dict) or not _valid_rect(rect):
        raise ExportError(
            "Crop must be a rectangle inside the video: numeric x, y, w, h in 0-1 "
            "with positive size."
        )
    return {k: round(float(rect[k]), 5) for k in ("x", "y", "w", "h")}


def crop_from_override(rect: dict, probe: "video.VideoProbe") -> "video.CropSettings":
    """Convert a normalized keep-region to pixel-inset ``CropSettings``."""
    width, height = probe.width, probe.height
    left = min(max(0, round(rect["x"] * width)), width - 2)
    top = min(max(0, round(rect["y"] * height)), height - 2)
    end_x = min(max(left + 2, round((rect["x"] + rect["w"]) * width)), width)
    end_y = min(max(top + 2, round((rect["y"] + rect["h"]) * height)), height)
    right = width - end_x
    bottom = height - end_y
    return video.CropSettings(left=left, top=top, right=right, bottom=bottom)


def get_crop_override(item_id: int) -> dict | None:
    """The item's saved crop keep-region, or ``None`` if it auto-crops."""
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise ExportError(f"No download item with id {item_id}.")
        return _parse_crop_override(item.crop_override)


def crop_preview_frame(item_id: int, at_seconds: float | None = None) -> Path:
    """Return a cached still frame used by the manual crop editor.

    ``at_seconds`` picks a specific moment (the crop editor's scrubber); ``None``
    keeps the default middle frame. Each timestamp caches to its own file so
    scrubbing back and forth stays cheap.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise ExportError(f"No download item with id {item_id}.")
        if not item.file_path:
            raise ExportError("This item has no downloaded video file to preview.")
        input_path = Path(item.file_path).expanduser().resolve()

    if not input_path.exists():
        raise ExportError(f"Video file not found: {input_path}")

    if at_seconds is None:
        name = f"item-{item_id}.jpg"
    else:
        name = f"item-{item_id}-t{int(max(at_seconds, 0.0) * 1000)}.jpg"
    preview_path = processed_dir() / "crop-previews" / name
    if not preview_path.exists() or preview_path.stat().st_mtime < input_path.stat().st_mtime:
        try:
            video.extract_video_preview_frame(input_path, preview_path, at_seconds)
        except Exception as exc:  # noqa: BLE001 - surface FFmpeg/probe failures as UI errors
            raise ExportError(f"Could not create crop preview: {exc}") from exc
    return preview_path


def source_duration_seconds(item_id: int) -> float | None:
    """Duration of an item's source clip, for the crop editor's scrubber range.

    Returns ``None`` when the item has no downloaded file yet or the duration
    can't be probed, so the UI simply hides the scrubber instead of failing.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise ExportError(f"No download item with id {item_id}.")
        if not item.file_path:
            return None
        input_path = Path(item.file_path).expanduser().resolve()
    if not input_path.exists():
        return None
    return video.probe_video(input_path).duration_seconds


def save_crop_override(item_id: int, rect: dict) -> dict:
    """Persist a manual crop keep-region for one item's export."""
    clean = _coerce_rect(rect)
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise ExportError(f"No download item with id {item_id}.")
        item.crop_override = json.dumps(clean)
        session.commit()
        return {"item_id": item_id, "crop_override": clean}


def clear_crop_override(item_id: int) -> dict:
    """Remove the manual crop so the item auto-crops again on export."""
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise ExportError(f"No download item with id {item_id}.")
        item.crop_override = None
        session.commit()
        return {"item_id": item_id, "crop_override": None}


def _replace_watermark_best_effort(output_path: Path, replacement_handle: str) -> dict:
    """Cover a foreign @handle watermark on the rendered reel with the publishing
    account's own handle.

    Best-effort, mirroring the legacy PyQt integration in
    ``app/main_window.py``: any failure — no detection, missing handle, an
    exception in the OCR/FFmpeg pipeline — leaves the rendered file untouched and
    only reports why, so watermarking NEVER fails an export. When a covered file
    is produced it atomically replaces the rendered output at the same path.
    """
    status = {
        "watermark_replaced": False,
        "watermark_detected_text": None,
        "watermark_skipped_reason": None,
    }
    if not replacement_handle:
        # No account handle to stamp — skip before the (expensive) detection pass.
        status["watermark_skipped_reason"] = "no publishing handle set"
        return status
    try:
        temp_output = output_path.with_stem(output_path.stem + "_watermark")
        replacement = replace_detected_watermark(
            output_path,
            replacement_text=replacement_handle,
            output_path=temp_output,
        )
        status["watermark_skipped_reason"] = replacement.skipped_reason
        status["watermark_detected_text"] = (
            replacement.region.text if replacement.region is not None else None
        )
        if replacement.output_path is not None:
            Path(replacement.output_path).replace(output_path)
            status["watermark_replaced"] = True
    except Exception:  # noqa: BLE001 - watermarking is a best-effort extra
        logger.exception("Watermark replacement failed for %s", output_path)
        status["watermark_skipped_reason"] = "watermark step failed"
    return status


def export_item(
    item_id: int,
    *,
    progress: ProgressFn | None = None,
    cancel_event: "threading.Event | None" = None,
) -> dict:
    """Render the processed Reel for ``item_id`` and set ``processed_path``.

    Returns ``{"item_id", "processed_path"}``, plus scheduling details or a
    non-fatal scheduling warning when the account enables auto-scheduling.
    Raises :class:`ExportError` for user-fixable export problems (no source
    file, file missing) so the message reaches the UI cleanly.

    ``cancel_event`` (injected by :class:`~nicheflow_studio.services.jobs.JobManager`
    when this runs as a background job) makes the export cancellable: it is
    checked between stages and passed to the FFmpeg render so an in-flight render
    is killed, raising :class:`~nicheflow_studio.services.jobs.JobCanceled`.
    """

    _check_cancel(cancel_event)
    with get_session() as session:
        pending_item = session.get(DownloadItem, item_id)
        should_download = bool(
            pending_item and pending_item.status == "pending_review" and not pending_item.file_path
        )
    if should_download:
        library.ensure_item_downloaded(item_id)

    def report(fraction: float, message: str = "") -> None:
        if progress is not None:
            progress(fraction, message)

    report(0.05, "Preparing export…")
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise ExportError(f"No download item with id {item_id}.")
        if not item.file_path:
            raise ExportError("This item has no downloaded video file to export.")
        input_path = Path(item.file_path)
        title_text = (item.title_draft or item.title or "").strip() or None
        override = _parse_crop_override(item.crop_override)
        # The publishing account's own @handle stamps over any foreign watermark.
        account = session.get(Account, item.account_id) if item.account_id is not None else None
        watermark_handle = (account.instagram_handle or "").strip() if account is not None else ""
        # Built here (not after the render config lookup) because it reads ORM
        # attributes that are unavailable once the session closes.
        post_header = build_post_header(account)

    resolved_input = input_path.expanduser()
    if not resolved_input.exists():
        raise ExportError(f"Video file not found: {input_path}")

    output_path = video.processed_output_path(input_path, processed_dir())

    report(0.15, "Analyzing video…")
    probe = video.probe_video(input_path)
    if override is not None:
        # Manual crop wins: the user fixed this clip's framing by hand.
        crop = crop_from_override(override, probe)
    else:
        try:
            crop = video.suggest_title_replacement_crop(input_path, probe)
        except Exception:  # noqa: BLE001 - crop detection is best-effort; fall back to no crop
            crop = video.CropSettings()

    _check_cancel(cancel_event)
    report(0.35, "Rendering reel…")
    render = render_config(item_id)
    try:
        result_path = video.export_cropped_video(
            input_path=input_path,
            output_path=output_path,
            crop=crop,
            title_text=title_text,
            title_layout=str(render.get("layout", "top_band")),
            title_font_size=int(render.get("font_size", 54)),
            title_font_name=str(render.get("font_name") or "arial_bold"),
            title_color=str(render.get("color", "#FFFFFF")),
            title_background=str(render.get("background", "none")),
            enable_bold_keywords=bool(render.get("bold_keywords", False)),
            title_align=str(render.get("align", "center")),
            title_line_gap_scale=render.get("line_gap_scale"),
            post_header=post_header if render.get("post_header") else None,
            cancel_event=cancel_event,
        )
    except video.RenderCanceled as exc:
        # The FFmpeg render was killed mid-flight; drop the partial output so a
        # canceled export never leaves a half-written file behind.
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove partial export output %s", output_path)
        raise JobCanceled(str(exc)) from exc

    _check_cancel(cancel_event)
    report(0.85, "Covering watermark…")
    watermark_status = _replace_watermark_best_effort(result_path, watermark_handle)

    _check_cancel(cancel_event)
    report(0.9, "Saving…")
    auto_schedule_on_export = False
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is not None:
            item.processed_path = str(result_path)
            session.commit()
            account = session.get(Account, item.account_id) if item.account_id is not None else None
            auto_schedule_on_export = bool(account and account.auto_schedule_on_export)

    result = {"item_id": item_id, "processed_path": str(result_path), **watermark_status}
    if auto_schedule_on_export:
        try:
            result["scheduled_publish"] = publishing.auto_schedule_for_publish(item_id)
        except publishing.PublishError as exc:
            result["warning"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - the export itself succeeded
            # A scheduling crash (e.g. a network error mid-cloud-handoff) must
            # not fail the whole export job: the reel is rendered and any local
            # schedule that was committed will be re-pushed by the cloud sync
            # sweep. Surface it the same way as a handled scheduling warning.
            logger.exception("Auto-schedule after export failed for item %s", item_id)
            result["warning"] = f"Auto-scheduling hit an unexpected error: {exc}"

    report(1.0, "Done")
    return result
