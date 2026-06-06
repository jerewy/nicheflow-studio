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

from pathlib import Path
from typing import Callable

from nicheflow_studio.core.paths import processed_dir
from nicheflow_studio.db.models import DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import video
from nicheflow_studio.services.errors import ServiceError

ProgressFn = Callable[[float, str], None]


class ExportError(ServiceError):
    """Raised when an item cannot be exported (missing file, ffmpeg, etc.)."""


def export_item(item_id: int, *, progress: ProgressFn | None = None) -> dict:
    """Render the processed Reel for ``item_id`` and set ``processed_path``.

    Returns ``{"item_id", "processed_path"}``. Raises :class:`ExportError` for
    user-fixable problems (no source file, file missing) so the message reaches
    the UI cleanly.
    """

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

    resolved_input = input_path.expanduser()
    if not resolved_input.exists():
        raise ExportError(f"Video file not found: {input_path}")

    output_path = video.processed_output_path(input_path, processed_dir())

    report(0.15, "Analyzing video…")
    probe = video.probe_video(input_path)
    try:
        crop = video.suggest_title_replacement_crop(input_path, probe)
    except Exception:  # noqa: BLE001 - crop detection is best-effort; fall back to no crop
        crop = video.CropSettings()

    report(0.35, "Rendering reel…")
    result_path = video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=crop,
        title_text=title_text,
        title_layout="top_band",
    )

    report(0.9, "Saving…")
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is not None:
            item.processed_path = str(result_path)
            session.commit()

    report(1.0, "Done")
    return {"item_id": item_id, "processed_path": str(result_path)}
