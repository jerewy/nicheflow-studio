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
    text_crop = detect_text_crop(resolved_input, probe)

    border_settings = border_crop or CropSettings()
    dark_band_settings = dark_band_crop or CropSettings()
    text_settings = text_crop or CropSettings()
    combined = CropSettings(
        left=max(border_settings.left, text_settings.left),
        top=max(border_settings.top, dark_band_settings.top, text_settings.top),
        right=max(border_settings.right, text_settings.right),
        bottom=max(border_settings.bottom, dark_band_settings.bottom, text_settings.bottom),
    )
    output_dimensions(probe, combined)

    reasons: list[str] = []
    if border_crop is not None and border_crop != CropSettings():
        reasons.append("removed detected border margins")
    if dark_band_crop is not None and dark_band_crop != CropSettings():
        reasons.append("trimmed repeated dark title bars around the active video area")
    if text_crop is not None and text_crop != CropSettings():
        reasons.append("trimmed repeated OCR text near the frame edges")
    if not reasons:
        reasons.append("no strong automatic crop signal detected")

    return CropSuggestion(
        crop=combined,
        reasons=tuple(reasons),
        used_border_detection=border_crop is not None or dark_band_crop is not None,
        used_ocr=text_crop is not None,
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
        start_ratio + ((end_ratio - start_ratio) * index / (count - 1))
        for index in range(count)
    ]
    return [max(duration * ratio, 0.0) for ratio in ratios]


def _extract_frame(ffmpeg_path: Path, input_path: Path, timestamp: float, output_path: Path) -> None:
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


def _extract_frame_image(ffmpeg_path: Path, input_path: Path, timestamp: float, output_path: Path) -> None:
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
        text=True,
        **subprocess_run_kwargs(),
    )
    return result.stdout


def _parse_tesseract_boxes(tsv_output: str) -> list[tuple[int, int, int, int]]:
    rows = [row for row in tsv_output.splitlines() if row.strip()]
    if len(rows) <= 1:
        return []

    boxes: list[tuple[int, int, int, int]] = []
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
        boxes.append((left, top, width, height))
    return boxes


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
    filter_parts = [f"crop={crop_width}:{crop_height}:{crop.left}:{crop.top}"]
    normalized_title = _normalize_overlay_text(title_text or "")
    font_path = windows_font_file(title_font_name)
    if normalized_title and font_path is not None:
        title_font_size, title_text_wrapped, title_box_height = _fit_title_overlay(
            normalized_title,
            crop_width=crop_width,
            requested_font_size=title_font_size,
        )
        wrapped_lines = [line.strip() for line in title_text_wrapped.split("\n") if line.strip()]
        line_spacing = max(8, int(title_font_size * 0.22))
        text_block_height = (len(wrapped_lines) * title_font_size) + (max(len(wrapped_lines) - 1, 0) * line_spacing)
        start_y = max(14, int((title_box_height - text_block_height) / 2))
        outline_width = 4 if title_font_name in {"comic_bold", "lilita_one_style", "impact", "grobold_style"} else 3
        if title_background == "dark":
            filter_parts.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=black@0.55:t=fill")
        elif title_background == "light":
            filter_parts.append(f"drawbox=x=0:y=0:w=iw:h={title_box_height}:color=white@0.35:t=fill")
        escaped_font = _escape_ffmpeg_path(font_path)
        for index, line_text in enumerate(wrapped_lines):
            escaped_text = _escape_drawtext(line_text)
            y_value = start_y + (index * (title_font_size + line_spacing))
            filter_parts.append(
                "drawtext="
                f"fontfile='{escaped_font}':"
                f"text='{escaped_text}':"
                f"fontcolor={title_color}:"
                f"fontsize={title_font_size}:"
                "x=(w-text_w)/2:"
                f"y={y_value}:"
                f"borderw={outline_width}:"
                "bordercolor=black@0.75"
            )
    crop_filter = ",".join(filter_parts)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(ffmpeg_path),
        "-y",
        "-i",
        str(resolved_input),
        "-vf",
        crop_filter,
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
        .replace("'", r"\'")
        .replace("%", r"\%")
        .replace("\n", r"\n")
    )


def _escape_ffmpeg_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:")


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
    sample_limit = min(int(height * 0.28), 240)
    rows = range(sample_limit) if from_top else range(height - 1, height - sample_limit - 1, -1)
    margin = 0
    started = False
    bright_gap = 0
    max_gap = 8
    for row_index in rows:
        row = frame_array[row_index]
        grayscale = (row[:, 0] * 0.299) + (row[:, 1] * 0.587) + (row[:, 2] * 0.114)
        dark_ratio = float((grayscale < 42).mean())
        mean_brightness = float(grayscale.mean())
        dark_band_row = dark_ratio >= 0.82 or (dark_ratio >= 0.35 and mean_brightness <= 70)
        if dark_band_row:
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
    box_height = max(64, int((font_size + 10) * line_count + 14))
    return font_size, wrapped, box_height


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
