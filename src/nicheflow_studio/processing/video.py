from __future__ import annotations

import json
import random
import re
import subprocess
import tempfile
import base64
import av
from dataclasses import dataclass
from statistics import median
from pathlib import Path

from nicheflow_studio.core.media_tools import (
    ffmpeg_binary,
    ffprobe_binary,
    subprocess_run_kwargs,
    tesseract_binary,
    windows_emoji_font_file,
    windows_font_file,
)


@dataclass(frozen=True)
class VideoProbe:
    width: int
    height: int
    duration_seconds: float | None


@dataclass(frozen=True)
class CropSettings:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0


@dataclass(frozen=True)
class AudioAlterParams:
    """Subtle audio alteration applied per export to keep copies non-identical.

    All defaults are no-ops, so an unset instance leaves audio unchanged.
    """

    tempo: float = 1.0
    pitch: float = 1.0
    delay_ms: int = 0
    volume: float = 1.0


@dataclass(frozen=True)
class CropSuggestion:
    crop: CropSettings
    reasons: tuple[str, ...]
    used_border_detection: bool
    used_ocr: bool


@dataclass(frozen=True)
class OcrTextBox:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class OcrRegionSignal:
    region: str
    timestamp: float
    text_detected: bool
    snippets: tuple[str, ...]
    average_confidence: float | None


@dataclass(frozen=True)
class PreprocessingOcrDiagnostics:
    top_text_detected: bool
    bottom_text_detected: bool
    top_snippets: tuple[str, ...]
    bottom_snippets: tuple[str, ...]
    average_confidence: float | None
    sample_count: int
    ffmpeg_available: bool
    tesseract_available: bool
    region_signals: tuple[OcrRegionSignal, ...]
    debug_messages: tuple[str, ...]


def probe_video(file_path: Path) -> VideoProbe:
    resolved_path = file_path.expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_path}")

    ffprobe_path = ffprobe_binary()
    if ffprobe_path is not None:
        try:
            command = [
                str(ffprobe_path),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(resolved_path),
            ]
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                **subprocess_run_kwargs(),
            )
            payload = json.loads(result.stdout)
            stream = (payload.get("streams") or [{}])[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width > 0 and height > 0:
                format_info = payload.get("format") or {}
                raw_duration = format_info.get("duration")
                duration_seconds = float(raw_duration) if raw_duration not in {None, ""} else None
                return VideoProbe(width=width, height=height, duration_seconds=duration_seconds)
        except Exception:  # noqa: BLE001
            pass

    return _probe_video_with_av(resolved_path)


def _probe_video_with_av(file_path: Path) -> VideoProbe:
    try:
        container = av.open(str(file_path))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Could not inspect the video file.") from exc

    try:
        stream = container.streams.video[0]
        width = int(getattr(stream, "width", 0) or 0)
        height = int(getattr(stream, "height", 0) or 0)
        if width < 1 or height < 1:
            raise RuntimeError("Could not determine the video dimensions.")

        duration_seconds: float | None = None
        stream_duration = getattr(stream, "duration", None)
        stream_time_base = getattr(stream, "time_base", None)
        if stream_duration is not None and stream_time_base is not None:
            duration_seconds = float(stream_duration * stream_time_base)
        elif getattr(container, "duration", None):
            duration_seconds = float(container.duration) / 1_000_000

        return VideoProbe(width=width, height=height, duration_seconds=duration_seconds)
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass


def output_dimensions(probe: VideoProbe, crop: CropSettings) -> tuple[int, int]:
    width = probe.width - crop.left - crop.right
    height = probe.height - crop.top - crop.bottom
    if width < 2 or height < 2:
        raise ValueError("Crop is too aggressive for this video.")
    if crop.left < 0 or crop.top < 0 or crop.right < 0 or crop.bottom < 0:
        raise ValueError("Crop values cannot be negative.")
    if crop.left >= probe.width or crop.right >= probe.width:
        raise ValueError("Left/right crop exceeds video width.")
    if crop.top >= probe.height or crop.bottom >= probe.height:
        raise ValueError("Top/bottom crop exceeds video height.")
    return (width, height)


def processed_output_path(input_path: Path, output_dir: Path) -> Path:
    resolved_input = input_path.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{resolved_input.stem}_cropped.mp4"


def suggest_crop_settings(input_path: Path) -> CropSuggestion:
    resolved_input = input_path.expanduser().resolve()
    probe = probe_video(resolved_input)
    border_crop = detect_border_crop(resolved_input, probe)
    dark_band_crop = detect_dark_band_crop(resolved_input, probe)
    safe_dark_band_crop = _safe_dark_band_crop(dark_band_crop, probe)
    top_text_crop = detect_top_title_crop(resolved_input, probe)
    bottom_text_crop = detect_bottom_caption_crop(resolved_input, probe)

    border_settings = border_crop or CropSettings()
    dark_band_settings = safe_dark_band_crop or CropSettings()
    text_settings = top_text_crop or CropSettings()
    bottom_text_settings = bottom_text_crop or CropSettings()
    combined = CropSettings(
        left=border_settings.left,
        top=max(border_settings.top, dark_band_settings.top, text_settings.top),
        right=border_settings.right,
        bottom=max(border_settings.bottom, dark_band_settings.bottom, bottom_text_settings.bottom),
    )
    output_dimensions(probe, combined)

    reasons: list[str] = []
    if border_crop is not None and border_crop != CropSettings():
        reasons.append("removed detected border margins")
    if safe_dark_band_crop is not None and safe_dark_band_crop != CropSettings():
        reasons.append("trimmed repeated dark title bars around the active video area")
    if top_text_crop is not None and top_text_crop != CropSettings():
        reasons.append("removed existing top title so the new title can replace it")
    if bottom_text_crop is not None and bottom_text_crop != CropSettings():
        reasons.append("removed repeated bottom caption text from the source video")
    if not reasons:
        reasons.append("no strong automatic crop signal detected")

    return CropSuggestion(
        crop=combined,
        reasons=tuple(reasons),
        used_border_detection=border_crop is not None or safe_dark_band_crop is not None,
        used_ocr=top_text_crop is not None or bottom_text_crop is not None,
    )


def detect_top_title_crop(input_path: Path, probe: VideoProbe) -> CropSettings | None:
    try:
        diagnostics = diagnose_preprocessing_ocr(input_path)
    except Exception:  # noqa: BLE001
        return None
    if not diagnostics.top_text_detected:
        return None

    text_crop = detect_text_crop(input_path, probe)
    if text_crop is None or text_crop.top < 1:
        return None
    return CropSettings(top=text_crop.top)


def detect_bottom_caption_crop(input_path: Path, probe: VideoProbe) -> CropSettings | None:
    try:
        diagnostics = diagnose_preprocessing_ocr(input_path)
    except Exception:  # noqa: BLE001
        return None
    if not diagnostics.bottom_text_detected:
        return None

    text_crop = detect_text_crop(input_path, probe)
    if text_crop is None or text_crop.bottom < 1:
        return None
    max_bottom_crop = int(probe.height * 0.18)
    if text_crop.bottom > max_bottom_crop:
        return None
    return CropSettings(bottom=text_crop.bottom)


def _safe_dark_band_crop(crop: CropSettings | None, probe: VideoProbe) -> CropSettings | None:
    if crop is None:
        return None
    max_vertical_margin = int(probe.height * 0.16)
    safe_crop = CropSettings(
        top=crop.top if 0 < crop.top <= max_vertical_margin else 0,
    )
    if safe_crop == CropSettings():
        return None
    return safe_crop


def suggest_title_replacement_crop(
    input_path: Path, probe: VideoProbe | None = None
) -> CropSettings:
    resolved_input = input_path.expanduser().resolve()
    video_probe = probe or probe_video(resolved_input)

    # Primary signal: isolate the embedded footage rectangle on all four sides.
    content_rect = detect_content_rectangle(resolved_input, video_probe)
    if content_rect is not None and content_rect != CropSettings():
        # Trust the detector's top edge as-is. The two-stage row scan in
        # detect_content_rectangle now terminates precisely at the first
        # non-content row above the footage, so an extra "safety" overshoot
        # is no longer needed and was clipping legitimate baked-in strips
        # like a "COURTROOM 1-5" banner at the top of the source clip.
        return content_rect

    # Fallback: older top-only detectors when no footage rectangle is found.
    max_top_crop = int(video_probe.height * 0.45)
    top_candidates: list[int] = []

    visual_crop = detect_visual_content_crop(resolved_input, video_probe)
    visual_top = (
        visual_crop.top if visual_crop is not None and 0 < visual_crop.top <= max_top_crop else 0
    )

    dark_band_crop = detect_dark_band_crop(resolved_input, video_probe)
    if dark_band_crop is not None and 0 < dark_band_crop.top <= max_top_crop:
        top_candidates.append(dark_band_crop.top)

    text_crop = detect_text_crop(resolved_input, video_probe)
    if text_crop is not None and 0 < text_crop.top <= max_top_crop:
        top_candidates.append(text_crop.top)

    if not top_candidates:
        if visual_top > 0:
            content_padding = max(16, int(video_probe.height * 0.015))
            return CropSettings(top=max(visual_top - content_padding, 0))
        return CropSettings()

    best = max(top_candidates)
    if visual_top > 0:
        content_padding = max(16, int(video_probe.height * 0.015))
        protected_top = max(visual_top - content_padding, 0)
        best = protected_top
    else:
        best += max(8, int(video_probe.height * 0.008))
    return CropSettings(top=min(best, max_top_crop))


def detect_visual_content_crop(input_path: Path, probe: VideoProbe) -> CropSettings | None:
    timestamps = _sample_timestamps(probe, count=5)
    if not timestamps:
        return None

    top_values: list[int] = []
    for timestamp in timestamps:
        frame = _load_video_frame_at(input_path, timestamp)
        if frame is None:
            continue
        top = _visual_content_top_margin(frame)
        if top is not None:
            top_values.append(top)

    top_margin = _bounded_margin(top_values)
    if top_margin < 1:
        return None
    return CropSettings(top=top_margin)


def _visual_content_top_margin(frame) -> int | None:  # noqa: ANN001
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        return None

    height, width = frame.shape[:2]
    if height < 2 or width < 2:
        return None

    gray = frame.astype(np.float32).mean(axis=2)
    row_coverage = (gray > 30).mean(axis=1)
    active_rows = row_coverage >= 0.45
    min_block_height = max(48, int(height * 0.08))

    start: int | None = None
    for index, is_active in enumerate(active_rows):
        if is_active and start is None:
            start = index
            continue
        if (not is_active or index == height - 1) and start is not None:
            end = index - 1 if not is_active else index
            block_height = end - start + 1
            if block_height >= min_block_height:
                return start
            start = None
    return None


# Tuning constants for content-rectangle detection. The embedded footage in an
# already-formatted meme video moves frame-to-frame; the black canvas and the
# static title/caption text do not. Temporal variance separates the two.
CONTENT_RECT_ACTIVITY_THRESHOLD = 7.0  # per-pixel std across sampled frames (0-255 luma)
CONTENT_RECT_BRIGHTNESS_THRESHOLD = 46.0  # mean luma above which a pixel is non-canvas
CONTENT_RECT_CANVAS_BORDER_RATIO = 0.02  # outer-frame sample used to estimate canvas luma
CONTENT_RECT_CANVAS_LUMA_TOLERANCE = 12.0  # light-canvas pixels within this range are canvas
CONTENT_RECT_LIGHT_CANVAS_MIN = 128.0  # preserve the tuned dark-canvas path byte-for-byte
CONTENT_RECT_COVERAGE_THRESHOLD = 0.18  # fraction of a row/column that must be "footage"
CONTENT_RECT_MIN_BAND_RATIO = 0.12  # shortest acceptable footage band per axis
CONTENT_RECT_MIN_REMAINING_RATIO = 0.08  # reject crops that leave < 8% of a dimension
# Bridge short inactive runs so a single visual content area broken by
# low-motion sub-regions (flat sky, ground, transient subtitles) does not
# get fragmented into separate bands. Static title/canvas bands have ~0
# coverage and produce gaps far larger than this, so they remain excluded.
CONTENT_RECT_MAX_GAP_RATIO = 0.08
# Top-edge overlay-text trim. Static white-on-canvas overlay text (e.g. a
# meme caption sitting just above the embedded clip) leaks tiny pixel-edge
# motion across to the rows that contain it, so the activity band's top
# boundary lands inside the text instead of below it. After the band is
# detected, scan the top portion for rows that are mostly "pure-white,
# zero-motion" pixels (true text), and push the top boundary past them.
CONTENT_RECT_TEXT_TOP_SCAN_RATIO = 0.30
CONTENT_RECT_TEXT_BRIGHTNESS_MIN = 230.0  # min brightness across all frames
CONTENT_RECT_TEXT_MOTION_MAX = 1.5  # per-pixel std must be near-zero
CONTENT_RECT_TEXT_ROW_RATIO = 0.05  # row needs >=5% text-like pixels to count
CONTENT_RECT_TEXT_MIN_ROWS = 6  # require a sustained text presence, not noise
# Blurred-background composition detection. Some reels embed a horizontal
# clip into a vertical frame by filling the surrounding canvas with a
# heavy-blurred copy of the same clip. Motion alone cannot crop those —
# the blurred canvas also "moves" — so they need a sharpness-aware path
# that locks onto the sharp inner rectangle.
CONTENT_RECT_SHARP_PIXEL_THRESHOLD = 1.0  # |Laplacian| above which a pixel is "sharp"
CONTENT_RECT_BLURRED_BG_MOTION_MIN = 0.20  # row motion-cov needed to qualify as blurred-bg
CONTENT_RECT_BLURRED_BG_SHARP_MAX = 0.05  # row sharp-cov ceiling for a "smooth" row
CONTENT_RECT_BLURRED_BG_MIN_ROW_RATIO = 0.05  # share of rows that must show the signature
CONTENT_RECT_BLURRED_BG_ROW_SHARP_MIN = 0.30  # clip row must reach this sharp-cov
CONTENT_RECT_BLURRED_BG_ROW_MOTION_MIN = 0.03  # ... and at least some motion
CONTENT_RECT_BLURRED_BG_COL_SHARP_MIN = 0.30  # clip column (within clip rows) sharp-cov
# Slab-restricted column detection (non-blurred-bg path). Compute column
# coverage over the rows of the detected row band only, instead of over the
# full image, then accept a column if EITHER it has motion OR sharp edges
# inside the band. Without this, multi-panel grids where some panels are
# static (4-up webcam splits) drop columns that contain only the still
# panels, leaving a narrow column band that crops out half the grid.
CONTENT_RECT_SLAB_COL_MOTION_MIN = 0.10
CONTENT_RECT_SLAB_COL_SHARP_MIN = 0.20
# Row-band edge fallback. Static rows that are still part of the source
# video (COURTROOM-style banner with white text on dark, "JUDGE PRESIDING"
# strips, baked-in date/time stamps) have zero motion but plenty of edges,
# so motion-only row detection drops them and the crop bites into real
# footage. A row counts as content if it has motion OR enough sharp pixels
# — UNLESS it also looks like a uniform-bright overlay bar (white/light
# meme caption strip), which the overlay-bar guard below catches.
CONTENT_RECT_ROW_SHARP_MIN = 0.18
# Overlay-bar fingerprint used to suppress uniform white/light static bars
# (e.g. clipsdailyxx-style title bars) that would otherwise pass the
# sharp-edge gate because of the text drawn on them.
CONTENT_RECT_OVERLAY_BAR_BRIGHTNESS_MIN = 200.0
CONTENT_RECT_OVERLAY_BAR_MOTION_MAX = 1.5
# The bottom edge often lands on the subtitle ink bbox. Keep a small guard so
# descenders/antialias pixels in baked-in captions ("g", "p", "y") survive.
CONTENT_RECT_BOTTOM_DESCENDER_PADDING_RATIO = 0.006


def _downscale_frame(frame, target_width: int):  # noqa: ANN001
    """Cheap stride-based downscale; good enough for coverage statistics."""
    height, width = frame.shape[:2]
    if width <= target_width:
        return frame
    step = max(1, width // target_width)
    return frame[::step, ::step, :]


def _largest_coverage_band(
    coverage, min_cov: float, min_len: int, *, max_gap: int = 0
) -> tuple[int, int] | None:  # noqa: ANN001
    """Longest near-contiguous run of indices whose coverage stays >= min_cov.

    Inactive runs up to ``max_gap`` long are bridged so a single visual band
    fragmented by low-motion sub-regions still registers as one run. With
    ``max_gap=0`` (default) the behaviour is strict contiguity.
    """
    best: tuple[int, int] | None = None
    best_len = 0
    start: int | None = None
    last_active: int | None = None
    gap_run = 0
    length = len(coverage)
    for index in range(length):
        is_active = bool(coverage[index] >= min_cov)
        if is_active:
            if start is None:
                start = index
            last_active = index
            gap_run = 0
            continue
        if start is None:
            continue
        gap_run += 1
        if gap_run > max_gap:
            run_len = last_active - start + 1
            if run_len > best_len:
                best_len = run_len
                best = (start, last_active)
            start = None
            last_active = None
            gap_run = 0
    if start is not None and last_active is not None:
        run_len = last_active - start + 1
        if run_len > best_len:
            best_len = run_len
            best = (start, last_active)
    if best is None or best_len < min_len:
        return None
    return best


def detect_content_rectangle(input_path: Path, probe: VideoProbe) -> CropSettings | None:
    """Find the embedded video footage rectangle inside a canvas/meme-format video.

    Returns a 4-sided crop isolating the footage, or None when no confident
    rectangle is found (e.g. a raw clip where footage already fills the frame).
    """
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        return None

    timestamps = _sample_timestamps(probe, count=7)
    if len(timestamps) < 2:
        return None

    frames = []
    for timestamp in timestamps:
        frame = _load_video_frame_at(input_path, timestamp)
        if frame is not None:
            frames.append(_downscale_frame(frame, target_width=480))
    if len(frames) < 2:
        return None

    min_h = min(frame.shape[0] for frame in frames)
    min_w = min(frame.shape[1] for frame in frames)
    if min_h < 4 or min_w < 4:
        return None
    stack = np.stack([frame[:min_h, :min_w, :] for frame in frames]).astype(np.float32)
    grays = stack.mean(axis=3)  # (N, h, w)

    temporal = grays.std(axis=0)  # motion map
    brightness = grays.mean(axis=0)  # average luma
    border_size = max(1, int(min(min_h, min_w) * CONTENT_RECT_CANVAS_BORDER_RATIO))
    border_pixels = np.concatenate(
        [
            grays[:, :border_size, :].reshape(-1),
            grays[:, -border_size:, :].reshape(-1),
            grays[:, border_size:-border_size, :border_size].reshape(-1),
            grays[:, border_size:-border_size, -border_size:].reshape(-1),
        ]
    )
    canvas_luma = float(np.median(border_pixels))
    is_light_canvas = canvas_luma >= CONTENT_RECT_LIGHT_CANVAS_MIN
    canvas_mask = (np.abs(brightness - canvas_luma) <= CONTENT_RECT_CANVAS_LUMA_TOLERANCE) & (
        temporal < CONTENT_RECT_ACTIVITY_THRESHOLD
    )
    # Active = MOVES (primary signal). Static graphics (title bars at top,
    # info captions at bottom) have zero motion and should NOT be treated
    # as footage to keep — they're overlays we want to crop away.
    # The previous logic `motion OR brightness` falsely tagged static white
    # title bars as "content" (white = 255 > 46), which is why videos from
    # accounts like clipsdailyxx (black-text-on-white title bands) kept the
    # title band in the cropped output. Dark static canvas areas were
    # excluded correctly (no motion + dark below threshold), but bright
    # static title bars survived the brightness OR — fixed here.
    # Brightness is now only a safety net for clips where the entire frame
    # has near-zero motion (pathological case — e.g. slideshow).
    has_any_motion = float(temporal.mean()) > 0.5
    if has_any_motion:
        active = temporal > CONTENT_RECT_ACTIVITY_THRESHOLD
    elif is_light_canvas:
        active = np.abs(brightness - canvas_luma) > CONTENT_RECT_CANVAS_LUMA_TOLERANCE
    else:
        active = brightness > CONTENT_RECT_BRIGHTNESS_THRESHOLD
    row_coverage = active.mean(axis=1)
    col_coverage = active.mean(axis=0)

    # Sharpness map (per-pixel absolute Laplacian over the temporal-mean frame).
    # Used both to detect blurred-background compositions and to size the inner
    # sharp rectangle for those compositions.
    lap = np.zeros_like(brightness)
    lap[1:-1, 1:-1] = (
        4 * brightness[1:-1, 1:-1]
        - brightness[:-2, 1:-1]
        - brightness[2:, 1:-1]
        - brightness[1:-1, :-2]
        - brightness[1:-1, 2:]
    )
    sharp_pixel = np.abs(lap) > CONTENT_RECT_SHARP_PIXEL_THRESHOLD
    if is_light_canvas:
        sharp_pixel &= ~canvas_mask
    row_sharp_cov = sharp_pixel.mean(axis=1)
    col_sharp_cov = sharp_pixel.mean(axis=0)
    # A row is "blurred-background" if it moves heavily yet is almost flat
    # (no edges). Real canvas/static rows have zero motion; real clip rows
    # have edges. Only the blurred-fill canvas pattern hits both criteria.
    blurred_bg_signature = (row_coverage >= CONTENT_RECT_BLURRED_BG_MOTION_MIN) & (
        row_sharp_cov < CONTENT_RECT_BLURRED_BG_SHARP_MAX
    )
    blurred_bg_count = int(blurred_bg_signature.sum())
    is_blurred_bg = blurred_bg_count >= max(30, int(min_h * CONTENT_RECT_BLURRED_BG_MIN_ROW_RATIO))

    if is_blurred_bg:
        # Vertical extent: rows that have BOTH sharp edges and at least a
        # trace of motion. Pure-static text rows fail the motion gate; pure
        # blurred-canvas rows fail the sharp gate.
        row_combined = (
            (row_sharp_cov >= CONTENT_RECT_BLURRED_BG_ROW_SHARP_MIN)
            & (row_coverage >= CONTENT_RECT_BLURRED_BG_ROW_MOTION_MIN)
        ).astype(np.float32)
        row_band = _largest_coverage_band(
            row_combined,
            0.5,
            max(1, int(min_h * CONTENT_RECT_MIN_BAND_RATIO)),
            max_gap=max(1, int(min_h * CONTENT_RECT_MAX_GAP_RATIO)),
        )
        if row_band is None:
            return None
        # Horizontal extent: sharpness coverage measured WITHIN the row band
        # only — column motion is unreliable for blurred-bg clips because
        # characters on the edges may be stationary, but they still belong to
        # the sharp rectangle.
        col_sharp_in_band = sharp_pixel[row_band[0] : row_band[1] + 1].mean(axis=0)
        col_combined = (col_sharp_in_band >= CONTENT_RECT_BLURRED_BG_COL_SHARP_MIN).astype(
            np.float32
        )
        col_band = _largest_coverage_band(
            col_combined,
            0.5,
            max(1, int(min_w * CONTENT_RECT_MIN_BAND_RATIO)),
            max_gap=max(1, int(min_w * CONTENT_RECT_MAX_GAP_RATIO)),
        )
    else:
        # Row detection in two stages:
        #   1. Find the largest contiguous MOTION band — this is the actual
        #      footage (e.g. judge + jersey moving). Motion is the trustworthy
        #      primary signal; the meme creator's overlay text sitting on the
        #      canvas above the video has no motion and won't be picked up.
        #   2. Extend up/down through DIRECTLY-ADJACENT rows that carry sharp
        #      edges. That catches static-but-baked-in strips that visually
        #      belong to the footage (a "COURTROOM 1-5" banner on dark, a
        #      "JUDGE PRESIDING" label, baked-in date/time stamps).
        # The extension stops at the first row that is neither sharp nor
        # moving, so a black gap between the overlay text and the footage
        # block correctly prevents the overlay from being absorbed. Rows
        # that look like uniform white/light overlay bars are skipped
        # entirely so the original clipsdailyxx-style title-bar protection
        # still holds.
        row_brightness_mean = brightness.mean(axis=1)
        row_motion_mean = temporal.mean(axis=1)
        is_overlay_bar_row = (row_brightness_mean > CONTENT_RECT_OVERLAY_BAR_BRIGHTNESS_MIN) & (
            row_motion_mean < CONTENT_RECT_OVERLAY_BAR_MOTION_MAX
        )
        motion_signal = (
            (row_coverage >= CONTENT_RECT_COVERAGE_THRESHOLD) & ~is_overlay_bar_row
        ).astype(np.float32)
        row_band = _largest_coverage_band(
            motion_signal,
            0.5,
            max(1, int(min_h * CONTENT_RECT_MIN_BAND_RATIO)),
            max_gap=max(1, int(min_h * CONTENT_RECT_MAX_GAP_RATIO)),
        )
        if row_band is None:
            return None
        # Extend through adjacent sharp rows in both directions. A single
        # blank/low-signal row breaks the extension so meme overlay text
        # separated from the footage by a black canvas gap is NOT absorbed.
        extend_top = row_band[0]
        while extend_top > 0:
            candidate = extend_top - 1
            if is_overlay_bar_row[candidate]:
                break
            if (
                row_sharp_cov[candidate] >= CONTENT_RECT_ROW_SHARP_MIN
                or row_coverage[candidate] >= CONTENT_RECT_COVERAGE_THRESHOLD
            ):
                extend_top = candidate
            else:
                break
        extend_bottom = row_band[1]
        while extend_bottom < min_h - 1:
            candidate = extend_bottom + 1
            if is_overlay_bar_row[candidate]:
                break
            if (
                row_sharp_cov[candidate] >= CONTENT_RECT_ROW_SHARP_MIN
                or row_coverage[candidate] >= CONTENT_RECT_COVERAGE_THRESHOLD
            ):
                extend_bottom = candidate
            else:
                break
        row_band = (extend_top, extend_bottom)
        # Column extent: measure motion AND sharpness over only the rows of the
        # detected band (canvas rows above/below dilute a full-image column
        # average, hiding still panels inside multi-panel grids). A column
        # counts as content if it has motion OR sharp edges inside the band.
        slab_motion_cov = active[row_band[0] : row_band[1] + 1].mean(axis=0)
        slab_sharp_cov = sharp_pixel[row_band[0] : row_band[1] + 1].mean(axis=0)
        col_signal = (
            (slab_motion_cov >= CONTENT_RECT_SLAB_COL_MOTION_MIN)
            | (slab_sharp_cov >= CONTENT_RECT_SLAB_COL_SHARP_MIN)
        ).astype(np.float32)
        col_band = _largest_coverage_band(
            col_signal,
            0.5,
            max(1, int(min_w * CONTENT_RECT_MIN_BAND_RATIO)),
            max_gap=max(1, int(min_w * CONTENT_RECT_MAX_GAP_RATIO)),
        )
    if row_band is None or col_band is None:
        return None

    # Refine the TOP edge by trimming overlay-bar rows that leak in. Limit
    # this to rows that are ALSO uniformly bright (white/light caption-bar
    # background), otherwise legitimate static-content strips like a dark
    # "COURTROOM 1-5" banner with white text would be trimmed too — that's
    # part of the source video, not an overlay, and chopping it produces
    # the over-aggressive crop seen on Memeists_Daily reels.
    top_band_index = row_band[0]
    brightness_min = grays.min(axis=0)
    static_text = (brightness_min > CONTENT_RECT_TEXT_BRIGHTNESS_MIN) & (
        temporal < CONTENT_RECT_TEXT_MOTION_MAX
    )
    if not is_blurred_bg:
        row_overlay_mask = is_overlay_bar_row
    else:
        row_overlay_mask = np.zeros(min_h, dtype=bool)
    scan_end = top_band_index + int(
        (row_band[1] - top_band_index) * CONTENT_RECT_TEXT_TOP_SCAN_RATIO
    )
    last_text_y = top_band_index - 1
    text_row_count = 0
    for scan_y in range(top_band_index, min(scan_end + 1, min_h)):
        if static_text[scan_y].mean() >= CONTENT_RECT_TEXT_ROW_RATIO and row_overlay_mask[scan_y]:
            last_text_y = scan_y
            text_row_count += 1
    if text_row_count >= CONTENT_RECT_TEXT_MIN_ROWS and last_text_y >= top_band_index:
        # Buffer past the last text row so descender/anti-alias pixels are gone.
        buffer = max(4, int(min_h * 0.008))
        top_band_index = min(last_text_y + 1 + buffer, scan_end)

    bottom_padding = max(4, int(probe.height * CONTENT_RECT_BOTTOM_DESCENDER_PADDING_RATIO))
    crop = CropSettings(
        left=int((col_band[0] / min_w) * probe.width),
        top=int((top_band_index / min_h) * probe.height),
        right=int(((min_w - 1 - col_band[1]) / min_w) * probe.width),
        bottom=max(0, int(((min_h - 1 - row_band[1]) / min_h) * probe.height) - bottom_padding),
    )
    remaining_w = probe.width - crop.left - crop.right
    remaining_h = probe.height - crop.top - crop.bottom
    if remaining_w < probe.width * CONTENT_RECT_MIN_REMAINING_RATIO:
        return None
    if remaining_h < probe.height * CONTENT_RECT_MIN_REMAINING_RATIO:
        return None
    return crop


def diagnose_preprocessing_ocr(
    input_path: Path, *, sample_count: int = 3
) -> PreprocessingOcrDiagnostics:
    resolved_input = input_path.expanduser().resolve()
    if not resolved_input.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_input}")

    ffmpeg_path = ffmpeg_binary()
    tesseract_path = tesseract_binary()
    if ffmpeg_path is None or tesseract_path is None:
        return PreprocessingOcrDiagnostics(
            top_text_detected=False,
            bottom_text_detected=False,
            top_snippets=(),
            bottom_snippets=(),
            average_confidence=None,
            sample_count=0,
            ffmpeg_available=ffmpeg_path is not None,
            tesseract_available=tesseract_path is not None,
            region_signals=(),
            debug_messages=tuple(
                message
                for message in (
                    "ffmpeg unavailable; cannot sample frames" if ffmpeg_path is None else "",
                    "Tesseract unavailable; cannot run OCR" if tesseract_path is None else "",
                )
                if message
            ),
        )

    probe = probe_video(resolved_input)
    timestamps = _sample_timestamps(probe, count=sample_count)
    if not timestamps:
        return PreprocessingOcrDiagnostics(
            top_text_detected=False,
            bottom_text_detected=False,
            top_snippets=(),
            bottom_snippets=(),
            average_confidence=None,
            sample_count=0,
            ffmpeg_available=True,
            tesseract_available=True,
            region_signals=(),
            debug_messages=("no sample timestamps available",),
        )

    top_snippets: list[str] = []
    bottom_snippets: list[str] = []
    all_confidences: list[float] = []
    region_signals: list[OcrRegionSignal] = []
    debug_messages: list[str] = []

    with tempfile.TemporaryDirectory(prefix="nicheflow-ocr-diagnostics-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, timestamp in enumerate(timestamps):
            for region in ("top", "bottom"):
                frame_path = temp_root / f"frame-{index}-{region}.png"
                _extract_frame_region(
                    ffmpeg_path, resolved_input, timestamp, probe, region, frame_path
                )
                boxes = _parse_tesseract_text_boxes(_run_tesseract_tsv(tesseract_path, frame_path))
                snippets = _ocr_snippets_from_boxes(boxes)
                confidences = [box.confidence for box in boxes]
                all_confidences.extend(confidences)
                if region == "top":
                    top_snippets.extend(snippets)
                else:
                    bottom_snippets.extend(snippets)
                region_signals.append(
                    OcrRegionSignal(
                        region=region,
                        timestamp=timestamp,
                        text_detected=bool(snippets),
                        snippets=tuple(snippets),
                        average_confidence=_average_confidence(confidences),
                    )
                )
                debug_messages.append(
                    f"{region}@{timestamp:.2f}s: {len(boxes)} text boxes, {len(snippets)} snippets"
                )

    top_unique = _confirmed_region_snippets(region_signals, "top", sample_count=len(timestamps))
    bottom_unique = _confirmed_region_snippets(
        region_signals, "bottom", sample_count=len(timestamps)
    )
    return PreprocessingOcrDiagnostics(
        top_text_detected=bool(top_unique),
        bottom_text_detected=bool(bottom_unique),
        top_snippets=tuple(top_unique),
        bottom_snippets=tuple(bottom_unique),
        average_confidence=_average_confidence(all_confidences),
        sample_count=len(timestamps),
        ffmpeg_available=True,
        tesseract_available=True,
        region_signals=tuple(region_signals),
        debug_messages=tuple(debug_messages),
    )


def detect_border_crop(input_path: Path, probe: VideoProbe) -> CropSettings | None:
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        return None

    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-i",
        str(input_path),
        "-vf",
        "fps=1,cropdetect=limit=0.08:round=2:reset=0",
        "-frames:v",
        "12",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        **subprocess_run_kwargs(),
    )
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr)
    if not matches:
        return None

    margins: list[CropSettings] = []
    for width_text, height_text, x_text, y_text in matches:
        width = int(width_text)
        height = int(height_text)
        x = int(x_text)
        y = int(y_text)
        right = max(probe.width - (x + width), 0)
        bottom = max(probe.height - (y + height), 0)
        margins.append(CropSettings(left=x, top=y, right=right, bottom=bottom))

    suggested = CropSettings(
        left=int(median([item.left for item in margins])),
        top=int(median([item.top for item in margins])),
        right=int(median([item.right for item in margins])),
        bottom=int(median([item.bottom for item in margins])),
    )
    if suggested == CropSettings():
        return None
    return suggested


def detect_text_crop(input_path: Path, probe: VideoProbe) -> CropSettings | None:
    ffmpeg_path = ffmpeg_binary()
    tesseract_path = tesseract_binary()
    if ffmpeg_path is None or tesseract_path is None:
        return None

    timestamps = _sample_timestamps(probe)
    if not timestamps:
        return None

    top_values: list[int] = []
    bottom_values: list[int] = []
    left_values: list[int] = []
    right_values: list[int] = []

    with tempfile.TemporaryDirectory(prefix="nicheflow-ocr-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, timestamp in enumerate(timestamps):
            frame_path = temp_root / f"frame-{index}.png"
            _extract_frame(ffmpeg_path, input_path, timestamp, frame_path)
            tsv_output = _run_tesseract_tsv(tesseract_path, frame_path)
            boxes = _parse_tesseract_boxes(tsv_output)
            for left, top, width, height in boxes:
                right = left + width
                bottom = top + height
                if top <= int(probe.height * 0.22):
                    top_values.append(min(bottom + 16, probe.height - 2))
                if bottom >= int(probe.height * 0.78):
                    bottom_values.append(min((probe.height - top) + 16, probe.height - 2))
                if left <= int(probe.width * 0.12):
                    left_values.append(min(right + 16, probe.width - 2))
                if right >= int(probe.width * 0.88):
                    right_values.append(min((probe.width - left) + 16, probe.width - 2))

    suggested = CropSettings(
        left=_bounded_margin(left_values),
        top=_bounded_margin(top_values),
        right=_bounded_margin(right_values),
        bottom=_bounded_margin(bottom_values),
    )
    if suggested == CropSettings():
        return None
    return suggested


def detect_dark_band_crop(input_path: Path, probe: VideoProbe) -> CropSettings | None:
    timestamps = _sample_timestamps(probe, count=5)
    if not timestamps:
        return None

    top_values: list[int] = []
    bottom_values: list[int] = []
    for timestamp in timestamps:
        frame = _load_video_frame_at(input_path, timestamp)
        if frame is None:
            continue
        top_margin = _dark_band_margin(frame, from_top=True)
        bottom_margin = _dark_band_margin(frame, from_top=False)
        if top_margin >= max(int(probe.height * 0.05), 24):
            top_values.append(top_margin)
        if bottom_margin >= max(int(probe.height * 0.05), 24):
            bottom_values.append(bottom_margin)

    suggested = CropSettings(
        top=_bounded_margin(top_values),
        bottom=_bounded_margin(bottom_values),
    )
    if suggested == CropSettings():
        return None
    return suggested


def sample_video_frame_data_urls(input_path: Path, *, max_frames: int = 5) -> list[str]:
    resolved_input = input_path.expanduser().resolve()
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        return []
    if not resolved_input.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_input}")

    probe = probe_video(resolved_input)
    timestamps = _sample_timestamps(probe, count=max_frames)
    if not timestamps:
        return []

    sampled_urls: list[str] = []
    with tempfile.TemporaryDirectory(prefix="nicheflow-frames-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, timestamp in enumerate(timestamps[:max_frames]):
            frame_path = temp_root / f"frame-{index}.jpg"
            _extract_frame_image(ffmpeg_path, resolved_input, timestamp, frame_path)
            sampled_urls.append(
                f"data:image/jpeg;base64,{base64.b64encode(frame_path.read_bytes()).decode('ascii')}"
            )
    return sampled_urls


def extract_video_preview_frame(input_path: Path, output_path: Path) -> Path:
    """Extract a middle-frame JPEG suitable for lightweight UI previews."""
    resolved_input = input_path.expanduser().resolve()
    resolved_output = output_path.expanduser().resolve()
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")
    if not resolved_input.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_input}")

    probe = probe_video(resolved_input)
    timestamps = _sample_timestamps(probe, count=1)
    timestamp = timestamps[0] if timestamps else 0.0
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    _extract_frame_image(ffmpeg_path, resolved_input, timestamp, resolved_output)
    return resolved_output


def _bounded_margin(values: list[int]) -> int:
    if not values:
        return 0
    return max(0, int(median(values)))


def _sample_timestamps(probe: VideoProbe, *, count: int = 3) -> list[float]:
    count = max(1, count)
    duration = probe.duration_seconds
    if duration is None or duration <= 0:
        return [0.0]
    if duration < 3:
        if count == 1:
            return [0.0]
        step = duration / max(count, 1)
        return [max(step * index, 0.0) for index in range(count)]

    start_ratio = 0.08
    end_ratio = 0.92
    if count == 1:
        return [max(duration * 0.5, 0.0)]

    ratios = [
        start_ratio + ((end_ratio - start_ratio) * index / (count - 1)) for index in range(count)
    ]
    return [max(duration * ratio, 0.0) for ratio in ratios]


def _extract_frame(
    ffmpeg_path: Path, input_path: Path, timestamp: float, output_path: Path
) -> None:
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )


def _extract_frame_region(
    ffmpeg_path: Path,
    input_path: Path,
    timestamp: float,
    probe: VideoProbe,
    region: str,
    output_path: Path,
) -> None:
    region_height = max(32, int(probe.height * 0.25))
    region_height = min(region_height, probe.height)
    y_offset = 0 if region == "top" else max(probe.height - region_height, 0)
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        "-vf",
        f"crop={probe.width}:{region_height}:0:{y_offset}",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )


def _extract_frame_image(
    ffmpeg_path: Path, input_path: Path, timestamp: float, output_path: Path
) -> None:
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2:force_original_aspect_ratio=decrease",
        "-q:v",
        "5",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )


def _run_tesseract_tsv(tesseract_path: Path, frame_path: Path) -> str:
    command = [
        str(tesseract_path),
        str(frame_path),
        "stdout",
        "--psm",
        "11",
        "tsv",
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        **subprocess_run_kwargs(),
    )
    return result.stdout


def _parse_tesseract_boxes(tsv_output: str) -> list[tuple[int, int, int, int]]:
    return [
        (box.left, box.top, box.width, box.height)
        for box in _parse_tesseract_text_boxes(tsv_output)
    ]


def _parse_tesseract_text_boxes(tsv_output: str) -> list[OcrTextBox]:
    rows = [row for row in tsv_output.splitlines() if row.strip()]
    if len(rows) <= 1:
        return []

    boxes: list[OcrTextBox] = []
    for row in rows[1:]:
        parts = row.split("\t")
        if len(parts) < 12:
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            confidence = float(parts[10])
            left = int(parts[6])
            top = int(parts[7])
            width = int(parts[8])
            height = int(parts[9])
        except ValueError:
            continue
        if confidence < 35 or width < 6 or height < 6:
            continue
        boxes.append(
            OcrTextBox(
                text=text,
                confidence=confidence,
                left=left,
                top=top,
                width=width,
                height=height,
            )
        )
    return boxes


def _ocr_snippets_from_boxes(boxes: list[OcrTextBox]) -> list[str]:
    words = [_clean_ocr_word(box.text) for box in boxes if box.confidence >= 45]
    words = [word for word in words if word]
    if not words:
        return []
    text = _normalize_overlay_text(" ".join(words))
    if not _is_meaningful_ocr_snippet(text):
        return []
    return [text[:140]]


def _clean_ocr_word(text: str) -> str:
    cleaned = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9?!'’-]+$", "", text.strip())
    if not cleaned:
        return ""
    alnum_count = sum(1 for character in cleaned if character.isalnum())
    alpha_count = sum(1 for character in cleaned if character.isalpha())
    if alnum_count < 2 and cleaned.casefold() not in {"a", "i"}:
        return ""
    if alpha_count == 0 and len(cleaned) < 3:
        return ""
    symbol_count = sum(1 for character in cleaned if not character.isalnum())
    if symbol_count > alnum_count:
        return ""
    return cleaned


def _is_meaningful_ocr_snippet(text: str) -> bool:
    words = [word for word in text.split() if word]
    if not words:
        return False
    alpha_count = sum(1 for character in text if character.isalpha())
    alnum_count = sum(1 for character in text if character.isalnum())
    if alpha_count < 7 and len(words) < 3:
        return False
    if alnum_count < 6:
        return False
    symbol_count = sum(
        1 for character in text if not character.isalnum() and not character.isspace()
    )
    if symbol_count > max(2, alnum_count // 2):
        return False
    short_words = [word for word in words if len(re.sub(r"[^A-Za-z0-9]", "", word)) <= 2]
    if len(words) > 1 and len(short_words) == len(words):
        return False
    if len(words) > 2 and len(short_words) > len(words) / 2:
        return False
    if len(words) == 2 and any(len(re.sub(r"[^A-Za-z0-9]", "", word)) < 3 for word in words):
        return False
    return True


def _confirmed_region_snippets(
    region_signals: list[OcrRegionSignal], region: str, *, sample_count: int
) -> list[str]:
    snippets: list[str] = []
    detected_samples = 0
    for signal in region_signals:
        if signal.region != region or not signal.snippets:
            continue
        detected_samples += 1
        snippets.extend(signal.snippets)
    if sample_count > 1 and detected_samples < 2:
        return []
    return _dedupe_snippets(snippets)


def _dedupe_snippets(snippets: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for snippet in snippets:
        normalized = _normalize_overlay_text(snippet).casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(snippet)
    return unique[:6]


def _average_confidence(confidences: list[float]) -> float | None:
    if not confidences:
        return None
    return round(sum(confidences) / len(confidences), 1)


# --- Audio alteration -------------------------------------------------------
# Light, randomized audio changes used when re-publishing the same source clip
# to multiple accounts. Goal: (1) make each account's copy a genuinely
# different file (different audio fingerprint), and (2) reduce the chance the
# upload reads as recycled. Bounds are deliberately small so spoken/historical
# audio stays fully intelligible.
AUDIO_ALTER_TEMPO_RANGE = (0.98, 1.02)
AUDIO_ALTER_PITCH_RANGE = (0.985, 1.015)
AUDIO_ALTER_DELAY_MS_RANGE = (0, 60)
AUDIO_ALTER_VOLUME_RANGE = (0.97, 1.03)
_AUDIO_SAMPLE_RATE = 44100
_VALID_AUDIO_MODES = {"keep", "alter"}


def random_audio_alter_params(rng: random.Random | None = None) -> AudioAlterParams:
    """Pick subtle, in-bounds audio alteration values.

    Pass a seeded ``random.Random`` (e.g. derived from an account id + clip id)
    when a copy must be regenerated identically.
    """
    chooser = rng or random.Random()
    return AudioAlterParams(
        tempo=round(chooser.uniform(*AUDIO_ALTER_TEMPO_RANGE), 4),
        pitch=round(chooser.uniform(*AUDIO_ALTER_PITCH_RANGE), 4),
        delay_ms=chooser.randint(*AUDIO_ALTER_DELAY_MS_RANGE),
        volume=round(chooser.uniform(*AUDIO_ALTER_VOLUME_RANGE), 4),
    )


def build_audio_filter(params: AudioAlterParams, *, sample_rate: int = _AUDIO_SAMPLE_RATE) -> str:
    """Build an ffmpeg audio filtergraph for the given alteration.

    Pitch is shifted via ``asetrate`` (which also changes speed); the sample
    rate is then restored and ``atempo`` corrects net duration back to the
    intended ``tempo``. An all-default ``AudioAlterParams`` yields ``anull``.
    """
    chain: list[str] = []
    if params.pitch != 1.0:
        chain.append(f"asetrate={int(round(sample_rate * params.pitch))}")
        chain.append(f"aresample={sample_rate}")
        net_tempo = params.tempo / params.pitch
    else:
        net_tempo = params.tempo
    if abs(net_tempo - 1.0) > 1e-4:
        chain.append(f"atempo={round(net_tempo, 6)}")
    if params.volume != 1.0:
        chain.append(f"volume={round(params.volume, 4)}")
    if params.delay_ms > 0:
        chain.append(f"adelay={params.delay_ms}:all=1")
    if not chain:
        return "anull"
    return ",".join(chain)


def has_audio_stream(file_path: Path) -> bool:
    """Return True if ``file_path`` has at least one audio stream.

    Falls back to True when ffprobe is unavailable so the caller defers to
    ffmpeg's optional ``0:a?`` mapping instead of wrongly dropping audio.
    """
    ffprobe_path = ffprobe_binary()
    if ffprobe_path is None:
        return True
    try:
        result = subprocess.run(
            [
                str(ffprobe_path),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(file_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            **subprocess_run_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return True
    return bool(result.stdout.strip())


def export_cropped_video(
    *,
    input_path: Path,
    output_path: Path,
    crop: CropSettings,
    title_text: str | None = None,
    title_font_size: int = 54,
    title_font_name: str | None = None,
    title_color: str = "#FFFFFF",
    title_background: str = "none",
    title_layout: str = "overlay",
    enable_bold_keywords: bool = False,
    audio_mode: str = "keep",
    audio_alter_params: AudioAlterParams | None = None,
) -> Path:
    resolved_input = input_path.expanduser().resolve()
    resolved_output = output_path.expanduser().resolve()
    if audio_mode not in _VALID_AUDIO_MODES:
        raise ValueError(
            f"Unknown audio_mode {audio_mode!r}; expected one of {sorted(_VALID_AUDIO_MODES)}."
        )
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")
    if not resolved_input.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_input}")

    probe = probe_video(resolved_input)
    crop_width, crop_height = output_dimensions(probe, crop)
    # ``**word**`` markers are only honoured by the black-canvas (top_band) PIL
    # renderer. They render as bold whenever they're present on a top_band
    # title — the marker IS the signal, so any black-canvas template (not just
    # the dedicated bold-keyword one) picks it up. ``enable_bold_keywords``
    # stays as an explicit opt-in. For every other layout, or when no markers
    # exist, strip them so they never paint literal asterisks onto a clip.
    title_has_markers = "**" in (title_text or "")
    keep_bold_markers = title_layout == "top_band" and (enable_bold_keywords or title_has_markers)
    raw_title = title_text or ""
    if not keep_bold_markers:
        raw_title = _strip_bold_markers(raw_title)
    normalized_title = _normalize_overlay_text(raw_title)
    font_path = windows_font_file(title_font_name)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nicheflow-title-") as title_temp_dir:
        title_text_dir = Path(title_temp_dir)
        # Pin the input resolution before cropping. Some sources (e.g. VP9) change
        # frame size mid-stream; without this the absolute crop offsets become
        # invalid on a filter reinit and ffmpeg aborts.
        normalize_scale = f"scale={probe.width}:{probe.height}"
        crop_filter = f"{normalize_scale},crop={crop_width}:{crop_height}:{crop.left}:{crop.top}"
        filter_parts = [crop_filter]
        complex_filter: str | None = None
        emoji_font_path = windows_emoji_font_file()
        if normalized_title and font_path is not None:
            if title_layout == "no_title":
                pass
            elif title_layout == "top_band":
                complex_filter = _title_band_filter_complex(
                    normalized_title,
                    crop=crop_filter,
                    crop_width=crop_width,
                    crop_height=crop_height,
                    font_path=font_path,
                    requested_font_size=title_font_size,
                    title_font_name=title_font_name,
                    title_color=title_color,
                    title_text_dir=title_text_dir,
                    duration_seconds=probe.duration_seconds,
                    enable_bold=keep_bold_markers,
                )
            elif _text_has_emoji(normalized_title) and emoji_font_path is not None:
                # Overlay layout with emoji needs filter_complex too — the
                # ``movie`` filter that loads the PIL-rendered PNG can't sit
                # inside a -vf chain alongside the cropped video stream.
                complex_filter = _title_overlay_filter_complex(
                    normalized_title,
                    crop=crop_filter,
                    crop_width=crop_width,
                    font_path=font_path,
                    emoji_font_path=emoji_font_path,
                    requested_font_size=title_font_size,
                    title_font_name=title_font_name,
                    title_color=title_color,
                    title_background=title_background,
                    title_text_dir=title_text_dir,
                )
            else:
                filter_parts.extend(
                    _title_overlay_filter_parts(
                        normalized_title,
                        crop_width=crop_width,
                        font_path=font_path,
                        requested_font_size=title_font_size,
                        title_font_name=title_font_name,
                        title_color=title_color,
                        title_background=title_background,
                        title_text_dir=title_text_dir,
                    )
                )

        audio_filter: str | None = None
        if audio_mode == "alter":
            resolved_audio_params = audio_alter_params or random_audio_alter_params()
            if has_audio_stream(resolved_input):
                audio_filter = build_audio_filter(resolved_audio_params)

        command = [
            str(ffmpeg_path),
            "-y",
            "-i",
            str(resolved_input),
        ]
        if complex_filter is not None:
            graph = complex_filter
            if audio_filter is not None:
                graph = f"{complex_filter};[0:a]{audio_filter}[aout]"
                audio_map = ["-map", "[aout]"]
            else:
                audio_map = ["-map", "0:a?"]
            command.extend(["-filter_complex", graph, "-map", "[vout]", *audio_map])
        else:
            command.extend(["-vf", ",".join(filter_parts)])
            if audio_filter is not None:
                command.extend(["-af", audio_filter])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(resolved_output),
            ]
        )
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            **subprocess_run_kwargs(),
        )
    return resolved_output


# Emoji codepoints. ffmpeg's `drawtext` filter loads exactly one font file
# and does no font fallback, so any emoji codepoint renders as the missing-
# glyph box (□) in the bundled Arial/Impact/Segoe UI Regular fonts. When an
# emoji-capable font (Windows Segoe UI Emoji) is available we render the
# title via Pillow to a transparent PNG and composite it with ffmpeg's
# `overlay` filter; when it isn't, we fall back to stripping the emoji.
_EMOJI_RUN_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # pictographs (😭, 💀, 🤔, 🔥, 🥹, 🤯, 💔, ...)
    "\U00002600-\U000027BF"  # misc symbols + dingbats (☀, ✨, ❤, ✅)
    "\U0001F1E6-\U0001F1FF"  # regional-indicator flags
    "\U0000FE0E-\U0000FE0F"  # variation selectors (text/emoji presentation)
    "\U0000200D"  # zero-width joiner (emoji sequences)
    "\U000020E3"  # combining enclosing keycap
    "]+"
)


def _strip_overlay_emojis(text: str) -> str:
    """Remove emoji codepoints AND clean the whitespace they leave behind.

    Used as the fallback when emoji rendering isn't available (non-Windows or
    Segoe UI Emoji missing). Collapses multi-space runs inside each line and
    trims per-line edges so a stripped trailing emoji doesn't leave a stray
    space. Paragraph breaks (``\\n`` / ``\\n\\n``) are preserved.
    """
    stripped = _EMOJI_RUN_PATTERN.sub("", text)
    lines = stripped.split("\n")
    cleaned = [re.sub(r"[ \t]+", " ", line).strip() for line in lines]
    return "\n".join(cleaned)


def _text_has_emoji(text: str) -> bool:
    return _EMOJI_RUN_PATTERN.search(text) is not None


def _split_emoji_runs(line: str) -> list[tuple[str, bool]]:
    """Split a line into ``(segment, is_emoji)`` runs in left-to-right order.

    Used by the PIL title renderer so each segment can be drawn with the
    font that actually contains its glyphs (Arial-family for text, Segoe UI
    Emoji for emoji codepoints).
    """
    runs: list[tuple[str, bool]] = []
    last_end = 0
    for match in _EMOJI_RUN_PATTERN.finditer(line):
        if match.start() > last_end:
            runs.append((line[last_end : match.start()], False))
        runs.append((match.group(0), True))
        last_end = match.end()
    if last_end < len(line):
        runs.append((line[last_end:], False))
    return runs


# ``**word**`` markdown-style emphasis markers. Only the dedicated bold-keyword
# template parses these; every other render path strips them up front so a
# stray marker never paints literal asterisks onto a clip.
_BOLD_MARKER_PATTERN = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

# Map each title font to the heaviest sibling that reads as "bold" against it.
# Unmapped fonts return ``None`` from ``_bold_font_file`` so bold emphasis
# degrades gracefully to the regular weight instead of a mismatched face.
_BOLD_FONT_SIBLINGS: dict[str, str] = {
    "georgia": "georgia_bold",
    "georgia_italic": "georgia_bold",
    "georgia_bold": "georgia_bold",
    "comic_italic": "comic_bold",
    "comic_bold": "comic_bold",
    "arial": "arial_bold",
    "arial_bold": "arial_black",
    "arial_rounded_bold": "arial_black",
}

# Bold keywords already swap to a heavier sibling face; enlarging them slightly
# on top of that makes the emphasis read clearly against body text instead of
# looking like the same size in a marginally darker weight.
_BOLD_KEYWORD_SCALE = 1.16


def _strip_bold_markers(text: str) -> str:
    """Remove ``**...**`` emphasis markers, keeping the wrapped words."""
    return _BOLD_MARKER_PATTERN.sub(r"\1", text)


def _parse_bold_markup(text: str) -> tuple[str, list[bool]]:
    """Split ``**word**`` markers out of ``text``.

    Returns the visible text (markers removed) and a parallel per-character
    list marking which visible characters are bold. Only well-formed ``**``
    pairs are treated as emphasis; an unmatched marker is left as literal text.
    """
    visible: list[str] = []
    bold_flags: list[bool] = []
    last = 0
    for match in _BOLD_MARKER_PATTERN.finditer(text):
        for char in text[last : match.start()]:
            visible.append(char)
            bold_flags.append(False)
        for char in match.group(1):
            visible.append(char)
            bold_flags.append(True)
        last = match.end()
    for char in text[last:]:
        visible.append(char)
        bold_flags.append(False)
    return "".join(visible), bold_flags


def _bold_masks_for_wrapped(
    visible_text: str, bold_flags: list[bool], wrapped_text: str
) -> list[list[bool]]:
    """Project per-character boldness from ``visible_text`` onto wrapped lines.

    The wrapper only regroups whole words (it never alters word content), so
    the non-whitespace characters of ``wrapped_text`` are exactly those of
    ``visible_text`` in the same order. A two-pointer walk over non-space
    characters carries each bold flag to its wrapped position; whitespace the
    wrapper inserts (single spaces, newlines) is always non-bold.
    """
    masks: list[list[bool]] = []
    visible_index = 0
    total = len(visible_text)
    for line in wrapped_text.split("\n"):
        mask: list[bool] = []
        for char in line:
            if char.isspace():
                mask.append(False)
                continue
            while visible_index < total and visible_text[visible_index].isspace():
                visible_index += 1
            mask.append(bold_flags[visible_index] if visible_index < total else False)
            visible_index += 1
        masks.append(mask)
    return masks


def _split_styled_runs(line: str, bold_mask: list[bool] | None) -> list[tuple[str, bool, bool]]:
    """Split a line into ``(segment, is_emoji, is_bold)`` runs.

    Generalises ``_split_emoji_runs`` so the PIL title renderer can also pick a
    bold font for emphasised characters. Consecutive characters sharing the
    same (emoji, bold) classification are merged into a single run.
    """
    emoji_chars = [False] * len(line)
    for match in _EMOJI_RUN_PATTERN.finditer(line):
        for index in range(match.start(), match.end()):
            emoji_chars[index] = True

    runs: list[tuple[str, bool, bool]] = []
    current = ""
    current_emoji: bool | None = None
    current_bold: bool | None = None
    for index, char in enumerate(line):
        is_emoji = emoji_chars[index]
        is_bold = bool(bold_mask[index]) if bold_mask and index < len(bold_mask) else False
        if current and is_emoji == current_emoji and is_bold == current_bold:
            current += char
        else:
            if current:
                runs.append((current, bool(current_emoji), bool(current_bold)))
            current, current_emoji, current_bold = char, is_emoji, is_bold
    if current:
        runs.append((current, bool(current_emoji), bool(current_bold)))
    return runs


def _bold_font_file(title_font_name: str | None) -> Path | None:
    """Resolve the bold sibling font for a title font, or ``None`` if unmapped."""
    sibling = _BOLD_FONT_SIBLINGS.get(title_font_name or "")
    if sibling is None:
        return None
    return windows_font_file(sibling)


def _color_to_rgba(color: str, *, alpha: int = 255) -> tuple[int, int, int, int]:
    """Convert ``#RRGGBB`` (or fallback to white) into a Pillow RGBA tuple."""
    cleaned = (color or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", cleaned):
        return (
            int(cleaned[1:3], 16),
            int(cleaned[3:5], 16),
            int(cleaned[5:7], 16),
            alpha,
        )
    return (255, 255, 255, alpha)


def _render_overlay_title_image(
    *,
    lines: list[str],
    canvas_width: int,
    canvas_height: int,
    font_path: Path,
    emoji_font_path: Path,
    font_size: int,
    line_spacing: int,
    color: str,
    start_y: int,
    align: str | int,
    outline_width: int,
    output_path: Path,
    bold_font_path: Path | None = None,
    bold_masks: list[list[bool]] | None = None,
) -> None:
    """Render a multi-line title with mixed text + emoji to a transparent PNG.

    Each line is split into emoji and non-emoji runs; non-emoji runs use the
    requested text font with a black stroke outline matching ``drawtext``'s
    ``borderw`` look, while emoji runs use Segoe UI Emoji via Pillow's
    ``embedded_color`` mode so COLR/CPAL palettes render in full color. The
    output PNG has alpha so ffmpeg's ``overlay`` filter composites it cleanly.

    ``align`` is either ``"center"`` (center each line on the canvas) or an
    integer pixel ``x`` to left-align every line (dialogue Template B uses
    this so the second paragraph hangs off the same column as the first).
    """
    from PIL import Image, ImageDraw, ImageFont

    text_font = ImageFont.truetype(str(font_path), font_size)
    emoji_font = ImageFont.truetype(str(emoji_font_path), font_size)
    bold_font = (
        ImageFont.truetype(str(bold_font_path), int(round(font_size * _BOLD_KEYWORD_SCALE)))
        if bold_font_path is not None
        else None
    )
    fill_rgba = _color_to_rgba(color)
    stroke_rgba = (0, 0, 0, 192)
    line_height = font_size + line_spacing
    # The enlarged bold font has a taller ascent, so drawing it at the same top
    # ``y`` as body text would push it below the shared baseline. Nudge bold
    # runs up by the ascent difference so every run sits on one baseline.
    base_ascent = text_font.getmetrics()[0]
    bold_baseline_dy = base_ascent - bold_font.getmetrics()[0] if bold_font is not None else 0

    def _font_for(is_emoji: bool, is_bold: bool):  # noqa: ANN202 - PIL font type
        if is_emoji:
            return emoji_font
        if is_bold and bold_font is not None:
            return bold_font
        return text_font

    image = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        y = start_y + index * line_height
        # Default (no bold mask) collapses to the original text/emoji split so
        # every existing caller renders byte-identically.
        if bold_font is not None:
            mask = bold_masks[index] if bold_masks and index < len(bold_masks) else None
            runs = _split_styled_runs(line, mask)
        else:
            runs = [(seg, emoji, False) for seg, emoji in _split_emoji_runs(line)]
        # Pre-measure each run so we know the line width before placing it.
        widths: list[int] = []
        for segment_text, segment_is_emoji, segment_is_bold in runs:
            font = _font_for(segment_is_emoji, segment_is_bold)
            widths.append(int(round(font.getlength(segment_text))))
        total_width = sum(widths)
        x = (canvas_width - total_width) // 2 if align == "center" else int(align)
        for (segment_text, segment_is_emoji, segment_is_bold), segment_width in zip(runs, widths):
            font = _font_for(segment_is_emoji, segment_is_bold)
            if segment_is_emoji:
                # embedded_color uses the font's COLR/CPAL palette and ignores
                # ``fill``; do not pass a stroke either — emojis carry their
                # own outline in the glyph design.
                draw.text((x, y), segment_text, font=font, embedded_color=True)
            else:
                run_y = y + bold_baseline_dy if font is bold_font else y
                draw.text(
                    (x, run_y),
                    segment_text,
                    font=font,
                    fill=fill_rgba,
                    stroke_width=outline_width,
                    stroke_fill=stroke_rgba,
                )
            x += segment_width
    image.save(output_path, "PNG")


def _normalize_overlay_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n+", normalized)
        if paragraph.strip()
    ]
    return "\n\n".join(paragraphs)


def _escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace(",", r"\,")
        .replace(";", r"\;")
        .replace("'", r"\\\'")
        .replace("%", r"\%")
        .replace("\n", r"\n")
    )


def _escape_ffmpeg_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:")


def _ffmpeg_drawtext_color(color: str) -> str:
    cleaned = color.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", cleaned):
        return f"0x{cleaned[1:]}"
    return cleaned or "white"


def _drawtext_textfile_option(line_text: str, title_text_dir: Path, file_stem: str) -> str:
    text_file = title_text_dir / f"{file_stem}.txt"
    text_file.write_text(line_text, encoding="utf-8")
    escaped_text_file = _escape_ffmpeg_path(text_file)
    return f"textfile='{escaped_text_file}':"


def _title_overlay_filter_parts(
    text: str,
    *,
    crop_width: int,
    font_path: Path,
    requested_font_size: int,
    title_font_name: str | None,
    title_color: str,
    title_background: str,
    title_text_dir: Path,
) -> list[str]:
    # When no emoji rendering is needed, callers reach this drawtext path.
    # Any emoji in the supplied text is stripped here as a fallback so the
    # rendered video doesn't show the missing-glyph box for emoji codepoints.
    safe_text = _strip_overlay_emojis(text) if _text_has_emoji(text) else text
    title_font_size, title_text_wrapped, title_box_height = _fit_title_overlay(
        safe_text,
        crop_width=crop_width,
        requested_font_size=requested_font_size,
    )
    # Keep blank-line positional placeholders so Template B titles render
    # their paragraph gap; see the matching comment in _title_band_filter_complex.
    raw_lines = title_text_wrapped.split("\n")
    visible_lines: list[tuple[int, str]] = [
        (index, line.strip()) for index, line in enumerate(raw_lines) if line.strip()
    ]
    line_count = max(len(raw_lines), 1)
    line_spacing = max(8, int(title_font_size * 0.22))
    text_block_height = (line_count * title_font_size) + (max(line_count - 1, 0) * line_spacing)
    start_y = max(32, int((title_box_height - text_block_height) / 2))
    outline_width = _title_outline_width(title_font_name)
    filter_parts: list[str] = []
    font_color = _ffmpeg_drawtext_color(title_color)
    if title_background == "dark":
        filter_parts.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=black@0.55:t=fill")
    elif title_background == "light":
        filter_parts.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=white@0.35:t=fill")
    escaped_font = _escape_ffmpeg_path(font_path)
    for chain_index, (original_index, line_text) in enumerate(visible_lines):
        text_option = _drawtext_textfile_option(line_text, title_text_dir, f"overlay-{chain_index}")
        y_value = start_y + (original_index * (title_font_size + line_spacing))
        filter_parts.append(
            "drawtext="
            f"fontfile='{escaped_font}':"
            f"{text_option}"
            f"fontcolor={font_color}:"
            f"fontsize={title_font_size}:"
            "x=(w-text_w)/2:"
            f"y={y_value}:"
            f"borderw={outline_width}:"
            "bordercolor=black@0.75"
        )
    return filter_parts


def _title_overlay_filter_complex(
    text: str,
    *,
    crop: str,
    crop_width: int,
    font_path: Path,
    emoji_font_path: Path,
    requested_font_size: int,
    title_font_name: str | None,
    title_color: str,
    title_background: str,
    title_text_dir: Path,
) -> str:
    """filter_complex variant of the overlay layout for emoji-bearing titles.

    The cropped frame is taken from ``[0:v]<crop>``, an optional translucent
    background slab is painted with ``drawbox``, and the PIL-rendered title
    PNG is composited on top with ``overlay``. The output label is ``[vout]``
    so the caller maps it through to the encoder.
    """
    title_font_size, title_text_wrapped, title_box_height = _fit_title_overlay(
        text,
        crop_width=crop_width,
        requested_font_size=requested_font_size,
    )
    raw_lines = title_text_wrapped.split("\n")
    line_count = max(len(raw_lines), 1)
    line_spacing = max(8, int(title_font_size * 0.22))
    text_block_height = (line_count * title_font_size) + (max(line_count - 1, 0) * line_spacing)
    start_y = max(32, int((title_box_height - text_block_height) / 2))
    outline_width = _title_outline_width(title_font_name)

    content_steps = [f"[0:v]{crop}"]
    if title_background == "dark":
        content_steps.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=black@0.55:t=fill")
    elif title_background == "light":
        content_steps.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=white@0.35:t=fill")
    content_chain = ",".join(content_steps) + "[content]"

    png_path = title_text_dir / "title-overlay.png"
    _render_overlay_title_image(
        lines=raw_lines,
        canvas_width=crop_width,
        canvas_height=title_box_height,
        font_path=font_path,
        emoji_font_path=emoji_font_path,
        font_size=title_font_size,
        line_spacing=line_spacing,
        color=title_color,
        start_y=start_y,
        align="center",
        outline_width=outline_width,
        output_path=png_path,
    )
    escaped_png = _escape_ffmpeg_path(png_path)
    # See the matching comment in _title_band_filter_complex: ``movie`` on a
    # still image is a single frame, and ``overlay`` repeats it for the
    # lifetime of the main stream by default.
    movie_chain = f"movie='{escaped_png}'[titletext]"
    return ";".join(
        [
            content_chain,
            movie_chain,
            "[content][titletext]overlay=0:0:format=auto,format=yuv420p[vout]",
        ]
    )


def _fit_within(
    src_width: int, src_height: int, box_width: int, box_height: int
) -> tuple[int, int]:
    """Largest even-dimension size that preserves aspect ratio and fits the box."""
    if src_width < 1 or src_height < 1:
        return (max(2, box_width - box_width % 2), max(2, box_height - box_height % 2))
    scale = min(box_width / src_width, box_height / src_height)
    width = max(2, int(src_width * scale))
    height = max(2, int(src_height * scale))
    return (width - width % 2, height - height % 2)


def _title_band_filter_complex(
    text: str,
    *,
    crop: str,
    crop_width: int,
    crop_height: int,
    font_path: Path,
    requested_font_size: int,
    title_font_name: str | None,
    title_color: str,
    title_text_dir: Path,
    duration_seconds: float | None,
    enable_bold: bool = False,
) -> str:
    canvas_width = 1080
    canvas_height = 1920
    emoji_font_path = windows_emoji_font_file()
    # Bold-keyword emphasis (``**word**``) is rendered through Pillow with a
    # bold sibling font; it requires both the emoji font (PIL path prerequisite)
    # and a mapped bold face, otherwise it degrades silently to plain text.
    bold_font_path = _bold_font_file(title_font_name) if enable_bold else None
    use_bold = enable_bold and bold_font_path is not None and emoji_font_path is not None
    if enable_bold:
        visible_text, bold_flags = _parse_bold_markup(text)
    else:
        visible_text, bold_flags = text, None
    # Render-through-Pillow is only possible when the emoji-capable font is
    # available; otherwise we strip emoji codepoints so drawtext doesn't emit
    # the missing-glyph box (□).
    use_emoji_path = _text_has_emoji(visible_text) and emoji_font_path is not None
    use_pil_path = use_emoji_path or use_bold
    band_text = visible_text if use_pil_path else _strip_overlay_emojis(visible_text)
    font_size, wrapped_text, title_band_height = _fit_title_band(
        band_text,
        canvas_width=canvas_width,
        requested_font_size=requested_font_size,
        title_font_name=title_font_name,
    )
    bold_masks = (
        _bold_masks_for_wrapped(visible_text, bold_flags, wrapped_text)
        if use_bold and bold_flags is not None
        else None
    )
    dialogue_title = _is_dialogue_meme_title(wrapped_text)
    # libx264 needs even dimensions; keep every stacked region even so the title
    # band, the content area, and their vstack all stay encodable.
    title_band_height -= title_band_height % 2
    video_height = canvas_height - title_band_height
    # Keep blank lines as positional placeholders so a Template B title
    # (``Them: X\n\nMe when Y:``) reserves the visual gap between its two
    # paragraphs. ``raw_lines`` drives line_count / y-positions; the
    # ``visible_lines`` list (with original index) drives drawtext chaining.
    raw_lines = wrapped_text.split("\n")
    visible_lines: list[tuple[int, str]] = [
        (index, line.strip()) for index, line in enumerate(raw_lines) if line.strip()
    ]
    line_count = max(len(raw_lines), 1)
    line_spacing = max(10, int(font_size * 0.24))
    text_block_height = (line_count * font_size) + (max(line_count - 1, 0) * line_spacing)
    bottom_gap = (
        max(76, int(font_size * 1.25))
        if _is_cinema_title_font(title_font_name)
        else max(14, min(28, int(font_size * 0.34)))
    )
    start_y = max(16, title_band_height - text_block_height - bottom_gap)
    escaped_font = _escape_ffmpeg_path(font_path)
    font_color = _ffmpeg_drawtext_color(title_color)
    title_duration = max(float(duration_seconds or 1.0), 0.1)

    # Scale the cropped footage to its exact fitted size (computed here, never an
    # `ih`-derived pad) so the graph is deterministic across source resolution
    # changes (e.g. VP9 mid-stream reinit). The title band sits directly above the
    # footage. Center the footage itself in the canvas; centering the combined
    # title+footage block pushes horizontal meme clips too low by half the title
    # band height.
    content_max_width = (
        _safe_inset_content_width(canvas_width)
        if _uses_safe_inset_content(title_font_name, requested_font_size=requested_font_size)
        else canvas_width
    )
    if _uses_safe_inset_content(title_font_name, requested_font_size=requested_font_size):
        # Scale to fill width, center-crop any excess height.
        # Wide sources (≥16:9) letterbox top/bottom as normal.
        # Narrow sources (9:16 Instagram reel with embedded movie) fill the
        # inset panel width — the side pillarboxing disappears without crossing
        # mobile safe-area edges.
        safe_w = content_max_width
        safe_scale = safe_w / max(1, crop_width)
        raw_h = int(crop_height * safe_scale)
        content_x = _safe_inset_margin_px(canvas_width)
        if raw_h > video_height:
            crop_top = (raw_h - video_height) // 2
            crop_top -= crop_top % 2
            content_height = video_height
            content_chain = (
                f"[0:v]{crop},"
                f"scale={safe_w}:-2,setsar=1,format=yuv420p,"
                f"crop={safe_w}:{content_height}:0:{crop_top},"
                f"pad={canvas_width}:{content_height}:{content_x}:0:color=black[content]"
            )
        else:
            content_height = raw_h - raw_h % 2
            content_chain = (
                f"[0:v]{crop},"
                f"scale={safe_w}:{content_height},setsar=1,format=yuv420p,"
                f"pad={canvas_width}:{content_height}:{content_x}:0:color=black[content]"
            )
    else:
        content_width, content_height = _fit_within(
            crop_width, crop_height, content_max_width, video_height
        )
        content_x = (canvas_width - content_width) // 2
        content_chain = (
            f"[0:v]{crop},"
            f"scale={content_width}:{content_height},setsar=1,format=yuv420p,"
            f"pad={canvas_width}:{content_height}:{content_x}:0:color=black[content]"
        )
    block_y = max(0, ((canvas_height - content_height) // 2) - title_band_height)
    titlebase = (
        f"color=c=black:s={canvas_width}x{title_band_height}:"
        f"r=30:d={title_duration:.3f}[titlebase]"
    )

    if use_pil_path:
        # PIL renders the whole title (text + emoji + bold keywords) onto a
        # transparent PNG the exact size of the title band, then ffmpeg's
        # ``overlay`` composites it onto the black band. This is the only way to
        # get color emoji or mixed font weights into the output — ffmpeg's
        # ``drawtext`` loads one font file per node with no fallback.
        png_path = title_text_dir / "title-band.png"
        align_value: str | int = max(72, content_x + 48) if dialogue_title else "center"
        _render_overlay_title_image(
            lines=raw_lines,
            canvas_width=canvas_width,
            canvas_height=title_band_height,
            font_path=font_path,
            emoji_font_path=emoji_font_path,
            font_size=font_size,
            line_spacing=line_spacing,
            color=title_color,
            start_y=start_y,
            align=align_value,
            outline_width=_title_outline_width(title_font_name),
            output_path=png_path,
            bold_font_path=bold_font_path if use_bold else None,
            bold_masks=bold_masks,
        )
        escaped_png = _escape_ffmpeg_path(png_path)
        # ``movie`` on a still PNG produces a single frame. ``overlay``'s
        # default ``eof_action=repeat`` holds that one frame for the full
        # lifetime of the main (titlebase) stream, so no explicit loop is
        # needed — using ``loop=-1`` here instead would make the secondary
        # stream infinite and ffmpeg would write an unbounded output.
        movie_chain = f"movie='{escaped_png}'[titletext]"
        overlay_chain = "[titlebase][titletext]overlay=0:0:format=auto,format=yuv420p[title]"
        return ";".join(
            [
                content_chain,
                titlebase,
                movie_chain,
                overlay_chain,
                "[title][content]vstack=inputs=2[block]",
                (
                    f"[block]pad={canvas_width}:{canvas_height}:"
                    f"(ow-iw)/2:{block_y}:color=black[vout]"
                ),
            ]
        )

    text_x = str(max(72, content_x + 48)) if dialogue_title else "(w-text_w)/2"
    filter_parts = [content_chain, titlebase]
    for chain_index, (original_index, line_text) in enumerate(visible_lines):
        text_option = _drawtext_textfile_option(
            line_text, title_text_dir, f"title-band-{chain_index}"
        )
        # y comes from the ORIGINAL line index so blank-line slots preserve
        # the visual gap; the filter chain itself only links non-empty
        # drawtext nodes via chain_index so [title0]/[title1]/... stay
        # contiguous and don't reference a skipped index.
        y_value = start_y + (original_index * (font_size + line_spacing))
        input_label = "titlebase" if chain_index == 0 else f"title{chain_index - 1}"
        output_label = "title" if chain_index == len(visible_lines) - 1 else f"title{chain_index}"
        filter_parts.append(
            f"[{input_label}]drawtext="
            f"fontfile='{escaped_font}':"
            f"{text_option}"
            f"fontcolor={font_color}:"
            f"fontsize={font_size}:"
            f"x={text_x}:"
            f"y={y_value}[{output_label}]"
        )
    filter_parts.append("[title][content]vstack=inputs=2[block]")
    filter_parts.append(
        f"[block]pad={canvas_width}:{canvas_height}:(ow-iw)/2:{block_y}:color=black[vout]"
    )
    return ";".join(filter_parts)


def _title_outline_width(title_font_name: str | None) -> int:
    return (
        4 if title_font_name in {"comic_bold", "lilita_one_style", "impact", "grobold_style"} else 3
    )


def _load_video_frame_at(input_path: Path, timestamp: float):
    try:
        container = av.open(str(input_path))
    except Exception:  # noqa: BLE001
        return None
    try:
        stream = container.streams.video[0]
        if stream.time_base is not None:
            target_pts = int((timestamp / float(stream.time_base)))
            try:
                container.seek(max(target_pts, 0), stream=stream, any_frame=False, backward=True)
            except Exception:  # noqa: BLE001
                pass
        for frame in container.decode(video=0):
            frame_time = float(frame.time) if frame.time is not None else 0.0
            if frame_time + 0.001 < timestamp:
                continue
            return frame.to_rgb().to_ndarray()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass
    return None


def _dark_band_margin(frame_array, *, from_top: bool) -> int:
    height = len(frame_array)
    if height < 2:
        return 0
    sample_limit = min(int(height * 0.42), 520)
    rows = range(sample_limit) if from_top else range(height - 1, height - sample_limit - 1, -1)
    margin = 0
    started = False
    bright_gap = 0
    max_gap = 18
    for row_index in rows:
        row = frame_array[row_index]
        grayscale = (row[:, 0] * 0.299) + (row[:, 1] * 0.587) + (row[:, 2] * 0.114)
        dark_ratio = float((grayscale < 42).mean())
        bright_ratio = float((grayscale > 185).mean())
        mean_brightness = float(grayscale.mean())
        dark_band_row = dark_ratio >= 0.82 or (dark_ratio >= 0.35 and mean_brightness <= 70)
        title_text_row = dark_ratio >= 0.12 and bright_ratio >= 0.12
        if dark_band_row or (started and title_text_row):
            started = True
            bright_gap = 0
            margin += 1
            continue
        if started and bright_gap < max_gap:
            bright_gap += 1
            margin += 1
            continue
        break
    return margin


def _fit_title_overlay(
    text: str,
    *,
    crop_width: int,
    requested_font_size: int,
) -> tuple[int, str, int]:
    # Initial sizing — preserves existing behavior for short overlay titles.
    # At crop_width=1080 this yields font up to 41 and max_chars 22.
    initial_font_size = min(requested_font_size, max(22, int(crop_width * 0.038)))
    initial_max_chars = max(12, min(22, int(crop_width / 36)))
    font_size = initial_font_size
    max_chars = initial_max_chars
    wrapped = _wrap_overlay_text(text, max_chars=max_chars, max_lines=2)
    # Phase A — prefer 2 balanced lines. Mirrors _fit_title_band.
    font_size_floor = 24
    while _wrapped_overflows(wrapped, max_chars) and font_size > font_size_floor:
        font_size = max(font_size_floor, font_size - 4)
        max_chars = max(
            max_chars,
            int(initial_max_chars * initial_font_size / max(1, font_size)),
        )
        wrapped = _wrap_overlay_text(text, max_chars=max_chars, max_lines=2)
    # Phase B — title doesn't fit 2 lines at the floor font; allow 3-4 lines.
    max_lines = 2
    while _wrapped_overflows(wrapped, max_chars) and max_lines < 4:
        max_lines += 1
        wrapped = _wrap_overlay_text(text, max_chars=max_chars, max_lines=max_lines)
    line_count = max(wrapped.count("\n") + 1, 1)
    box_height = max(96, int((font_size + 14) * line_count + 34))
    return font_size, wrapped, box_height


def _fit_title_band(
    text: str,
    *,
    canvas_width: int,
    requested_font_size: int,
    title_font_name: str | None = None,
) -> tuple[int, str, int]:
    dialogue_title = _is_dialogue_meme_title(text)
    if dialogue_title:
        font_size = min(max(requested_font_size, 42), max(42, int(canvas_width * 0.049)), 54)
        max_chars = max(22, min(34, int(canvas_width / 32)))
        wrapped = _wrap_overlay_text(text, max_chars=max_chars, max_lines=2)
    elif _is_cinema_title_font(title_font_name):
        # Cinema title treatment needs a narrower text measure than meme
        # templates. Keep a natural long-first-line pull quote instead of
        # balancing lines into a justified-looking block.
        #
        # Body text stays compact (~46px at 1080px) so the cinema pull quote
        # reads small against the frame; emphasis comes from enlarging only the
        # bold keyword (see ``_BOLD_KEYWORD_SCALE``), not the whole caption.
        font_size = min(max(46, int(canvas_width * 0.043)), requested_font_size, 54)
        max_chars = max(30, min(44, int(canvas_width / 30)))
        wrap_func = _wrap_overlay_text_greedy
        wrapped = wrap_func(text, max_chars=max_chars, max_lines=2)
        # Allow shrinking further before adding a third line so long pull
        # quotes (~80-90 chars) wrap to a tidy 2-line block instead of an
        # orphaned third line.
        font_size_floor = 38
        while (
            _wrapped_overflows(wrapped, max_chars)
            or _wrapped_exceeds_safe_width(
                wrapped,
                font_size=font_size,
                title_font_name=title_font_name,
                canvas_width=canvas_width,
            )
        ) and font_size > font_size_floor:
            font_size = max(font_size_floor, font_size - 4)
            max_chars = max(
                max_chars,
                min(48, int(canvas_width / max(1, int(font_size * 0.58)))),
            )
            wrapped = wrap_func(text, max_chars=max_chars, max_lines=2)
        max_lines = 2
        while _wrapped_overflows(wrapped, max_chars) and max_lines < 4:
            max_lines += 1
            wrapped = wrap_func(text, max_chars=max_chars, max_lines=max_lines)
    else:
        # Initial sizing for standard/editorial title templates.
        # Past Moments Black uses a slightly larger title while keeping
        # the wider, natural line wrapping of the cinema title style.
        font_size = min(max(requested_font_size, 50), max(50, int(canvas_width * 0.052)), 60)

        # Match the wider text measure used by Cinema Bold Keywords.
        max_chars = max(30, min(44, int(canvas_width / 30)))

        # Use natural/greedy wrapping instead of balanced wrapping so the
        # title does not force itself into two equally sized lines.
        wrap_func = _wrap_overlay_text_greedy
        wrapped = wrap_func(text, max_chars=max_chars, max_lines=2)

        # Prefer 2 lines; shrink only when the title cannot fit safely.
        font_size_floor = 38
        while (
            _wrapped_overflows(wrapped, max_chars)
            or _wrapped_exceeds_safe_width(
                wrapped,
                font_size=font_size,
                title_font_name=title_font_name,
                canvas_width=canvas_width,
            )
        ) and font_size > font_size_floor:
            font_size = max(font_size_floor, font_size - 4)
            max_chars = max(
                max_chars,
                min(48, int(canvas_width / max(1, int(font_size * 0.58)))),
            )
            wrapped = wrap_func(text, max_chars=max_chars, max_lines=2)

        max_lines = 2
        while _wrapped_overflows(wrapped, max_chars) and max_lines < 4:
            max_lines += 1
            wrapped = wrap_func(text, max_chars=max_chars, max_lines=max_lines)
    line_count = max(wrapped.count("\n") + 1, 1)
    line_spacing = max(10, int(font_size * 0.24))
    text_height = (line_count * font_size) + (max(line_count - 1, 0) * line_spacing)
    vertical_padding = (
        max(72, int(canvas_width * 0.067))
        if dialogue_title
        else max(168, int(canvas_width * 0.156))
        if _is_cinema_title_font(title_font_name)
        else max(118, int(canvas_width * 0.11))
    )
    # Single/double-line titles keep the original 320px cap so existing
    # outputs are unchanged. 3-line auto-fit gets 420; 4-line gets 520 so
    # the smaller-font fallback isn't squeezed. Dialogue Template B titles
    # already used 480 for line_count >= 3 — that range still applies via
    # the 420/520 tiers (420 for 3 lines, 520 for 4) and the cap is the
    # ceiling, not the height, so dialogue bands are unchanged in practice.
    if line_count <= 2:
        max_band_height = 320
    elif line_count == 3:
        max_band_height = 420
    else:
        max_band_height = 520
    band_height = max(170, min(max_band_height, text_height + vertical_padding))
    return font_size, wrapped, band_height


def _is_cinema_title_font(title_font_name: str | None) -> bool:
    return title_font_name in {"comic_italic", "arial_rounded_bold", "georgia"}


def _uses_safe_inset_content(
    title_font_name: str | None, *, requested_font_size: int | None = None
) -> bool:
    if title_font_name in {
        "comic_italic",
        "arial_rounded_bold",
        "georgia",
        "past_moments_arial_bold",
    }:
        return True
    # Backward compatibility for accounts that saved Past Moments Black before
    # it got its own internal font key. Other Arial Bold templates use 56-64px.
    return title_font_name == "arial_bold" and (requested_font_size or 0) <= 46


_CINEMA_SIDE_MARGIN_PX = 56  # exact pixels on EACH side — left == right by construction
# Bumped from 32 to 56 (~5% of a 1080 canvas) so the footage sits a little
# further from the frame edges and stops getting clipped on smaller phone
# screens with rounded corners / safe-area insets.
_TITLE_SAFE_SIDE_MARGIN_PX = 72


def _safe_inset_content_width(canvas_width: int) -> int:
    width = canvas_width - _CINEMA_SIDE_MARGIN_PX * 2
    return width - (width % 2)  # keep even for H.264


def _safe_inset_margin_px(canvas_width: int) -> int:
    """Return the exact left=right side margin so the caller never re-derives it."""
    return (canvas_width - _safe_inset_content_width(canvas_width)) // 2


def _title_safe_text_width(canvas_width: int) -> int:
    return max(1, canvas_width - (_TITLE_SAFE_SIDE_MARGIN_PX * 2))


def _wrapped_exceeds_safe_width(
    wrapped: str,
    *,
    font_size: int,
    title_font_name: str | None,
    canvas_width: int,
) -> bool:
    """True when any rendered title line would cross the left/right safe area."""
    if title_font_name is None:
        return False
    safe_width = _title_safe_text_width(canvas_width)
    for line in wrapped.split("\n"):
        if not line.strip():
            continue
        if (
            _title_line_pixel_width(line, font_size=font_size, title_font_name=title_font_name)
            > safe_width
        ):
            return True
    return False


def _title_line_pixel_width(line: str, *, font_size: int, title_font_name: str | None) -> int:
    try:
        from PIL import ImageFont

        font_path = windows_font_file(title_font_name)
        if font_path is not None:
            font = ImageFont.truetype(str(font_path), font_size)
            bbox = font.getbbox(line)
            return max(0, int(bbox[2] - bbox[0]))
    except Exception:  # noqa: BLE001 - keep renderer usable without Pillow/font metrics
        pass
    factor = 0.62 if title_font_name in {"arial_bold", "arial_rounded_bold", "impact"} else 0.58
    return int(len(line) * font_size * factor)


def _wrapped_overflows(wrapped: str, max_chars: int) -> bool:
    """Return True if any non-empty line in ``wrapped`` exceeds ``max_chars``.

    Used by the title-band auto-fit loop to detect when
    ``_wrap_single_paragraph`` had to collapse excess lines onto the last
    line, which produces a line that clips against the canvas edges.
    """
    return any(len(line) > max_chars for line in wrapped.split("\n") if line.strip())


def _is_dialogue_meme_title(text: str) -> bool:
    if "\n\n" not in text:
        return False
    visible_lines = [line.strip().casefold() for line in text.splitlines() if line.strip()]
    if len(visible_lines) < 2:
        return False
    has_response_line = any(line == "me:" or line.startswith("me:") for line in visible_lines)
    has_setup_line = any(":" in line and not line.startswith("me:") for line in visible_lines)
    return has_response_line and has_setup_line


def _wrap_overlay_text(text: str, *, max_chars: int, max_lines: int) -> str:
    """Wrap title text to ``max_chars`` columns, ``max_lines`` per paragraph.

    Explicit ``\\n\\n`` paragraph breaks in the input are preserved — each
    paragraph is wrapped independently and the pieces are rejoined with a
    blank line. This is what makes Template B meme titles (``Them: "X"\\n\\n
    Me when Y:``) render as two visually separated lines on the video
    overlay instead of being collapsed into one word-wrapped block by a
    single global ``text.split()``.

    Each paragraph is wrapped with the balanced (min-max) algorithm so the
    resulting lines are as close in width as possible — that's the
    cinema.defined aesthetic. When no wrapping fits inside ``max_chars``
    at the allowed line count, falls back to greedy wrap with collapse so
    the caller's auto-fit loop can detect the overflow via
    ``_wrapped_overflows`` and retry with smaller font / more lines.
    """
    if not text:
        return ""
    # Split on explicit paragraph breaks. Empty paragraphs (eg. triple
    # newlines from a sloppy model output) are dropped so we never emit
    # consecutive blank lines.
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""
    wrapped_paragraphs: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            continue
        balanced = _balanced_wrap_lines(words, max_chars=max_chars, max_lines=max_lines)
        if balanced is not None:
            wrapped_paragraphs.append("\n".join(balanced))
        else:
            wrapped_paragraphs.append(
                _wrap_single_paragraph(paragraph, max_chars=max_chars, max_lines=max_lines)
            )
    # Reassemble with a single blank line between paragraphs so the
    # downstream renderer's split("\n") yields the empty-string slot it
    # needs to space the second paragraph correctly.
    return "\n\n".join(wrapped_paragraphs)


def _wrap_overlay_text_greedy(text: str, *, max_chars: int, max_lines: int) -> str:
    """Wrap each paragraph greedily instead of balancing every line.

    The soft cinema template looks closer to the reference when the first
    line can carry the idea and the second line is a shorter centered payoff.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""
    return "\n\n".join(
        _wrap_single_paragraph(paragraph, max_chars=max_chars, max_lines=max_lines)
        for paragraph in paragraphs
        if paragraph.split()
    )


def _balanced_wrap_lines(words: list[str], *, max_chars: int, max_lines: int) -> list[str] | None:
    """Wrap ``words`` into 1..``max_lines`` lines each <= ``max_chars`` chars,
    minimizing the maximum line width so output looks visually balanced
    instead of left-packed.

    Returns ``None`` when no valid wrapping exists at these constraints —
    the caller falls back to greedy-with-collapse so the auto-fit loop
    can notice the overflow and retry with a smaller font / more lines.

    On ties (multiple line counts achieve the same min max-width), the
    smaller line count wins so we don't fragment text into more lines
    than necessary.
    """
    n = len(words)
    if n == 0:
        return []
    if max_chars <= 0 or max_lines <= 0:
        return None
    word_lens = [len(w) for w in words]

    # widths[j][i] = len(" ".join(words[j:i])) for j < i. Precomputed
    # because the DP loop accesses it O(max_lines * n^2) times.
    widths = [[0] * (n + 1) for _ in range(n + 1)]
    for j in range(n):
        running = 0
        for i in range(j + 1, n + 1):
            running += word_lens[i - 1]
            if i - j > 1:
                running += 1  # space between words
            widths[j][i] = running

    inf = float("inf")
    # dp[k][i] = minimum achievable max-line-width to fit words[0:i] in
    # exactly k lines; parent[k][i] is the j where the k-th line begins.
    dp: list[list[float]] = [[inf] * (n + 1) for _ in range(max_lines + 1)]
    parent: list[list[int]] = [[-1] * (n + 1) for _ in range(max_lines + 1)]
    dp[0][0] = 0

    for k in range(1, max_lines + 1):
        for i in range(1, n + 1):
            for j in range(0, i):
                if dp[k - 1][j] >= inf:
                    continue
                line_w = widths[j][i]
                if line_w > max_chars:
                    continue
                cand = max(dp[k - 1][j], line_w)
                if cand < dp[k][i]:
                    dp[k][i] = cand
                    parent[k][i] = j

    # Prefer the FEWEST number of lines that fits. "Focus Mode Activated"
    # fits cleanly on one line; a min-max tie-breaker would split it into
    # "Focus Mode" / "Activated" because two 10-char lines minimize
    # max-width better than one 20-char line. That's the wrong tradeoff —
    # short titles should stay on one line, not get fragmented. Within the
    # chosen line count, the DP still finds the most-balanced split.
    chosen_k = -1
    for k in range(1, max_lines + 1):
        if dp[k][n] < inf:
            chosen_k = k
            break
    if chosen_k < 0:
        return None

    lines: list[str] = []
    i = n
    k = chosen_k
    while k > 0:
        j = parent[k][i]
        lines.append(" ".join(words[j:i]))
        i = j
        k -= 1
    return list(reversed(lines))


def _wrap_single_paragraph(text: str, *, max_chars: int, max_lines: int) -> str:
    """Greedy word-wrap with collapse — fallback used by ``_wrap_overlay_text``
    when ``_balanced_wrap_lines`` can't satisfy the constraints.

    Wraps a single paragraph into at most ``max_lines`` lines of ``max_chars``
    columns each. Excess content is collapsed onto the last line so callers
    still see every word; the resulting overflow trips ``_wrapped_overflows``
    in the auto-fit loop, which then retries with smaller font / more lines.
    """
    words = text.split()
    if not words:
        return ""

    lines: list[str] = []
    current: list[str] = []
    current_length = 0
    for word in words:
        projected = current_length + (1 if current else 0) + len(word)
        if current and projected > max_chars:
            lines.append(" ".join(current))
            current = [word]
            current_length = len(word)
        else:
            current.append(word)
            current_length = projected
    if current:
        lines.append(" ".join(current))
    if len(lines) > max_lines:
        collapsed = " ".join(lines[max_lines - 1 :])
        lines = lines[: max_lines - 1] + [collapsed]
    return "\n".join(lines[:max_lines])
