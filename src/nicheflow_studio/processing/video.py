from __future__ import annotations

import json
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
        # Small top overshoot so any source sub-line text is fully gone.
        top_overshoot = int(video_probe.height * 0.02)
        max_top = int(video_probe.height * 0.85)
        return CropSettings(
            left=content_rect.left,
            top=min(content_rect.top + top_overshoot, max_top),
            right=content_rect.right,
            bottom=content_rect.bottom,
        )

    # Fallback: older top-only detectors when no footage rectangle is found.
    max_top_crop = int(video_probe.height * 0.45)
    top_candidates: list[int] = []

    visual_crop = detect_visual_content_crop(resolved_input, video_probe)
    visual_top = (
        visual_crop.top
        if visual_crop is not None and 0 < visual_crop.top <= max_top_crop
        else 0
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
CONTENT_RECT_COVERAGE_THRESHOLD = 0.18  # fraction of a row/column that must be "footage"
CONTENT_RECT_MIN_BAND_RATIO = 0.12  # shortest acceptable footage band per axis
CONTENT_RECT_MIN_REMAINING_RATIO = 0.08  # reject crops that leave < 8% of a dimension


def _downscale_frame(frame, target_width: int):  # noqa: ANN001
    """Cheap stride-based downscale; good enough for coverage statistics."""
    height, width = frame.shape[:2]
    if width <= target_width:
        return frame
    step = max(1, width // target_width)
    return frame[::step, ::step, :]


def _largest_coverage_band(
    coverage, min_cov: float, min_len: int
) -> tuple[int, int] | None:  # noqa: ANN001
    """Longest contiguous run of indices whose coverage stays >= min_cov."""
    best: tuple[int, int] | None = None
    best_len = 0
    start: int | None = None
    length = len(coverage)
    for index in range(length):
        is_active = bool(coverage[index] >= min_cov)
        if is_active and start is None:
            start = index
        if (not is_active or index == length - 1) and start is not None:
            end = index if (is_active and index == length - 1) else index - 1
            run_len = end - start + 1
            if run_len > best_len:
                best_len = run_len
                best = (start, end)
            start = None
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
    active = (temporal > CONTENT_RECT_ACTIVITY_THRESHOLD) | (
        brightness > CONTENT_RECT_BRIGHTNESS_THRESHOLD
    )
    row_coverage = active.mean(axis=1)
    col_coverage = active.mean(axis=0)

    row_band = _largest_coverage_band(
        row_coverage,
        CONTENT_RECT_COVERAGE_THRESHOLD,
        max(1, int(min_h * CONTENT_RECT_MIN_BAND_RATIO)),
    )
    col_band = _largest_coverage_band(
        col_coverage,
        CONTENT_RECT_COVERAGE_THRESHOLD,
        max(1, int(min_w * CONTENT_RECT_MIN_BAND_RATIO)),
    )
    if row_band is None or col_band is None:
        return None

    crop = CropSettings(
        left=int((col_band[0] / min_w) * probe.width),
        top=int((row_band[0] / min_h) * probe.height),
        right=int(((min_w - 1 - col_band[1]) / min_w) * probe.width),
        bottom=int(((min_h - 1 - row_band[1]) / min_h) * probe.height),
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
) -> Path:
    resolved_input = input_path.expanduser().resolve()
    resolved_output = output_path.expanduser().resolve()
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")
    if not resolved_input.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_input}")

    probe = probe_video(resolved_input)
    crop_width, crop_height = output_dimensions(probe, crop)
    normalized_title = _normalize_overlay_text(title_text or "")
    font_path = windows_font_file(title_font_name)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nicheflow-title-") as title_temp_dir:
        title_text_dir = Path(title_temp_dir)
        # Pin the input resolution before cropping. Some sources (e.g. VP9) change
        # frame size mid-stream; without this the absolute crop offsets become
        # invalid on a filter reinit and ffmpeg aborts.
        normalize_scale = f"scale={probe.width}:{probe.height}"
        crop_filter = (
            f"{normalize_scale},crop={crop_width}:{crop_height}:{crop.left}:{crop.top}"
        )
        filter_parts = [crop_filter]
        top_band_filter: str | None = None
        if normalized_title and font_path is not None:
            if title_layout == "no_title":
                pass
            elif title_layout == "top_band":
                top_band_filter = _title_band_filter_complex(
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

        command = [
            str(ffmpeg_path),
            "-y",
            "-i",
            str(resolved_input),
        ]
        if top_band_filter is not None:
            command.extend(
                [
                    "-filter_complex",
                    top_band_filter,
                    "-map",
                    "[vout]",
                    "-map",
                    "0:a?",
                ]
            )
        else:
            command.extend(["-vf", ",".join(filter_parts)])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
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


def _normalize_overlay_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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
    title_font_size, title_text_wrapped, title_box_height = _fit_title_overlay(
        text,
        crop_width=crop_width,
        requested_font_size=requested_font_size,
    )
    wrapped_lines = [line.strip() for line in title_text_wrapped.split("\n") if line.strip()]
    line_spacing = max(8, int(title_font_size * 0.22))
    text_block_height = (len(wrapped_lines) * title_font_size) + (
        max(len(wrapped_lines) - 1, 0) * line_spacing
    )
    start_y = max(32, int((title_box_height - text_block_height) / 2))
    outline_width = _title_outline_width(title_font_name)
    filter_parts: list[str] = []
    font_color = _ffmpeg_drawtext_color(title_color)
    if title_background == "dark":
        filter_parts.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=black@0.55:t=fill")
    elif title_background == "light":
        filter_parts.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=white@0.35:t=fill")
    escaped_font = _escape_ffmpeg_path(font_path)
    for index, line_text in enumerate(wrapped_lines):
        text_option = _drawtext_textfile_option(line_text, title_text_dir, f"overlay-{index}")
        y_value = start_y + (index * (title_font_size + line_spacing))
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
) -> str:
    canvas_width = 1080
    canvas_height = 1920
    font_size, wrapped_text, title_band_height = _fit_title_band(
        text,
        canvas_width=canvas_width,
        requested_font_size=requested_font_size,
    )
    # libx264 needs even dimensions; keep every stacked region even so the title
    # band, the content area, and their vstack all stay encodable.
    title_band_height -= title_band_height % 2
    video_height = canvas_height - title_band_height
    wrapped_lines = [line.strip() for line in wrapped_text.split("\n") if line.strip()]
    line_spacing = max(10, int(font_size * 0.24))
    text_block_height = (len(wrapped_lines) * font_size) + (
        max(len(wrapped_lines) - 1, 0) * line_spacing
    )
    bottom_gap = max(14, min(28, int(font_size * 0.34)))
    start_y = max(16, title_band_height - text_block_height - bottom_gap)
    escaped_font = _escape_ffmpeg_path(font_path)
    font_color = _ffmpeg_drawtext_color(title_color)
    title_duration = max(float(duration_seconds or 1.0), 0.1)

    # Scale the cropped footage to its exact fitted size (computed here, never an
    # `ih`-derived pad) so the graph is deterministic across source resolution
    # changes (e.g. VP9 mid-stream reinit). The title band sits directly above the
    # footage, and the combined block is centered in the canvas so the title stays
    # close to the video instead of floating with a large gap.
    content_width, content_height = _fit_within(
        crop_width, crop_height, canvas_width, video_height
    )
    content_x = (canvas_width - content_width) // 2
    filter_parts = [
        (
            f"[0:v]{crop},"
            f"scale={content_width}:{content_height},setsar=1,format=yuv420p,"
            f"pad={canvas_width}:{content_height}:{content_x}:0:color=black[content]"
        ),
        f"color=c=black:s={canvas_width}x{title_band_height}:r=30:d={title_duration:.3f}[titlebase]",
    ]
    for index, line_text in enumerate(wrapped_lines):
        text_option = _drawtext_textfile_option(line_text, title_text_dir, f"title-band-{index}")
        y_value = start_y + (index * (font_size + line_spacing))
        input_label = "titlebase" if index == 0 else f"title{index - 1}"
        output_label = "title" if index == len(wrapped_lines) - 1 else f"title{index}"
        filter_parts.append(
            f"[{input_label}]drawtext="
            f"fontfile='{escaped_font}':"
            f"{text_option}"
            f"fontcolor={font_color}:"
            f"fontsize={font_size}:"
            "x=(w-text_w)/2:"
            f"y={y_value}[{output_label}]"
        )
    filter_parts.append("[title][content]vstack=inputs=2[block]")
    filter_parts.append(
        f"[block]pad={canvas_width}:{canvas_height}:(ow-iw)/2:(oh-ih)/2:color=black[vout]"
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
    font_size = min(requested_font_size, max(22, int(crop_width * 0.038)))
    max_chars = max(12, min(22, int(crop_width / 36)))
    wrapped = _wrap_overlay_text(text, max_chars=max_chars, max_lines=2)
    line_count = max(wrapped.count("\n") + 1, 1)
    box_height = max(96, int((font_size + 14) * line_count + 34))
    return font_size, wrapped, box_height


def _fit_title_band(
    text: str,
    *,
    canvas_width: int,
    requested_font_size: int,
) -> tuple[int, str, int]:
    font_size = min(max(requested_font_size, 44), max(44, int(canvas_width * 0.06)), 76)
    max_chars = max(18, min(34, int(canvas_width / 34)))
    wrapped = _wrap_overlay_text(text, max_chars=max_chars, max_lines=2)
    line_count = max(wrapped.count("\n") + 1, 1)
    line_spacing = max(10, int(font_size * 0.24))
    text_height = (line_count * font_size) + (max(line_count - 1, 0) * line_spacing)
    vertical_padding = max(118, int(canvas_width * 0.11))
    band_height = max(170, min(320, text_height + vertical_padding))
    return font_size, wrapped, band_height


def _wrap_overlay_text(text: str, *, max_chars: int, max_lines: int) -> str:
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
