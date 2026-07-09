"""Watermark detection and brand-cover replacement for downloaded reels.

Detects @username watermarks using the Tesseract OCR pipeline already present
in video.py. Replaces them with a solid cover box and optional branded text
using FFmpeg.

No new dependencies — builds entirely on existing project tools.

Typical usage
-------------
    from nicheflow_studio.processing.watermark import detect_watermark, cover_watermark

    region = detect_watermark(Path("clip.mp4"))
    if region:
        result = cover_watermark(Path("clip.mp4"), region, replacement_text="@meme.ig")
        # result.output_path → clip_branded.mp4
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.media_tools import (
    ffmpeg_binary,
    subprocess_run_kwargs,
    tesseract_binary,
    windows_font_file,
)
from nicheflow_studio.processing.video import (
    OcrTextBox,
    VideoProbe,
    _parse_tesseract_text_boxes,
    _run_tesseract_tsv,
    _sample_timestamps,
    probe_video,
)


_HANDLE_RE = re.compile(r"@[A-Za-z0-9_.]{2,30}", re.IGNORECASE)

# Fraction of the frame taken as the corner crop region
_CORNER_WIDTH_RATIO = 0.40
_CORNER_HEIGHT_RATIO = 0.20

# Minimum crop dimensions for very small videos
_MIN_CORNER_W = 80
_MIN_CORNER_H = 24

# Tesseract confidence threshold — boxes below this are ignored
_MIN_CONFIDENCE = 25

# Pixels added around the detected box for the cover rectangle
_BOX_PADDING = 8

_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
_MIN_HANDLE_BODY_CHARS = 4


@dataclass(frozen=True)
class WatermarkRegion:
    """A detected @handle watermark in full-video pixel coordinates."""

    x: int       # left edge
    y: int       # top edge
    w: int       # width
    h: int       # height
    text: str    # detected handle e.g. "@epicfunnypage"
    corner: str  # which corner: "top-left" / "top-right" / "bottom-left" / "bottom-right"


@dataclass(frozen=True)
class WatermarkCoverResult:
    """Result of a cover_watermark() call."""

    output_path: Path
    region: WatermarkRegion
    replacement_text: str | None


@dataclass(frozen=True)
class WatermarkReplacementResult:
    """Result of safe watermark replacement, including skipped cases."""

    output_path: Path | None
    region: WatermarkRegion | None
    replacement_text: str
    skipped_reason: str | None = None


@dataclass(frozen=True)
class _WatermarkCoverGeometry:
    x: int
    y: int
    w: int
    h: int
    text_x: int
    text_y: int
    font_size: int
    corner_radius: int  # used by _rounded_rect_filter for the geq background


def detect_watermark(video_path: Path, *, sample_count: int = 3) -> WatermarkRegion | None:
    """Sample frames, OCR likely watermark regions, return the detected @handle region.

    Returns None if:
    - FFmpeg or Tesseract is not available
    - The video file does not exist or cannot be probed
    - No @handle watermark is found in any corner

    The detection samples ``sample_count`` evenly-spaced frames and checks
    strict corners plus broader content-edge regions where repost watermarks
    often sit under title bands. The most consistently detected handle wins.

    Two-pass strategy
    -----------------
    Pass 1 (raw): OCR on the unmodified crop — works for normal solid watermarks.
    Pass 2 (enhanced): if pass 1 finds nothing, retry every region with a
    contrast-boosted, greyscale, 2x-upscaled crop. This makes semi-transparent
    or low-contrast watermarks (e.g. ``@clips`` style) readable by Tesseract
    without touching the cover/replacement logic at all.
    """
    ffmpeg_path = ffmpeg_binary()
    tesseract_path = tesseract_binary()
    if ffmpeg_path is None or tesseract_path is None:
        return None

    resolved = video_path.expanduser().resolve()
    if not resolved.exists():
        return None

    try:
        probe = probe_video(resolved)
    except Exception:  # noqa: BLE001
        return None

    timestamps = _sample_timestamps(probe, count=sample_count)
    if not timestamps:
        return None

    scan_regions = _watermark_scan_regions(probe)

    with tempfile.TemporaryDirectory(prefix="nicheflow-wm-") as tmp:
        tmp_root = Path(tmp)

        # Pass 1: raw crop — fast path, handles normal solid-contrast watermarks.
        detections = _ocr_pass(
            ffmpeg_path, tesseract_path, resolved,
            timestamps, scan_regions, tmp_root,
            enhance=False, label="raw",
        )

        # Pass 2: enhanced crop — only runs when pass 1 found nothing.
        # Uses negate+brightness-lift so Tesseract can read light-on-light
        # watermarks (white text on sky/content) that pass 1 missed.
        # Also uses more sample timestamps: the enhanced pass is slower per
        # frame, but since it only fires when pass 1 found nothing, the extra
        # frames are only paid for genuinely hard cases. More samples reduce
        # the chance of every timestamp landing in a gap where the watermark
        # is not on screen (e.g. during scene cuts or animated intros).
        if not detections:
            enhanced_timestamps = _sample_timestamps(
                probe, count=max(5, sample_count * 2)
            )
            detections = _ocr_pass(
                ffmpeg_path, tesseract_path, resolved,
                enhanced_timestamps, scan_regions, tmp_root,
                enhance=True, label="enh",
            )

    if not detections:
        return None

    return _best_watermark_detection(detections)


def cover_watermark(
    video_path: Path,
    region: WatermarkRegion,
    *,
    replacement_text: str | None = None,
    output_path: Path | None = None,
) -> WatermarkCoverResult:
    """Cover the watermark region with a solid box and optional branded text.

    Runs a single FFmpeg pass: drawbox covers the detected area, drawtext
    (optional) overlays the replacement handle. Audio is copied without
    re-encoding.

    Args:
        video_path:       Source video file.
        region:           Region returned by detect_watermark().
        replacement_text: Text to render over the cover box e.g. "@meme.ig".
                          Pass None to leave the box empty.
        output_path:      Where to write the result. Defaults to the source
                          path with a ``_branded`` suffix.

    Raises:
        RuntimeError: If FFmpeg is not available.
        subprocess.CalledProcessError: If the FFmpeg command fails.
    """
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("FFmpeg not found — cannot cover watermark.")

    resolved = video_path.expanduser().resolve()
    if output_path is None:
        output_path = resolved.with_stem(resolved.stem + "_branded")

    try:
        probe = probe_video(resolved)
    except Exception:  # noqa: BLE001
        probe = None

    geometry = _cover_geometry(
        region,
        replacement_text=replacement_text,
        probe=probe,
    )

    # Rounded-rectangle background using geq (per-pixel math).
    # This is the only pure-FFmpeg way to get soft rounded corners —
    # drawbox is always square. Opacity at 0.72 fully hides the original
    # watermark without creating a harsh painted slab.
    filters: list[str] = [
        _rounded_rect_filter(
            geometry.x, geometry.y, geometry.w, geometry.h,
            radius=geometry.corner_radius,
            opacity=1.0,  # full black — nothing from the original watermark shows through
        ),
    ]

    if replacement_text:
        font_arg = _font_arg()
        safe_text = replacement_text.replace("'", "\\'").replace(":", "\\:")
        # Plain white Arial Black, like Instagram's own handle stamp. The pill
        # underneath is fully opaque black, so an outline or shadow cannot add
        # contrast — at badge font sizes they only bleed dark fringes into the
        # glyph antialiasing and make the handle look smudged.
        filters.append(
            f"drawtext={font_arg}text='{safe_text}'"
            f":x={geometry.text_x}:y={geometry.text_y}"
            f":fontsize={geometry.font_size}:fontcolor=white"
        )

    # Encode settings must mirror export_cropped_video. Left to libx264
    # defaults, the geq rounded-rect filter promotes the stream to yuv444p
    # (High 4:4:4 Predictive), which the WebView2 preview and Instagram both
    # reject or degrade, and the moov atom lands at the end of the file.
    # Audio is re-encoded (not copied): copying has produced a truncated
    # final AAC packet that stops playback with a decode error.
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-i", str(resolved),
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )
    return WatermarkCoverResult(
        output_path=output_path,
        region=region,
        replacement_text=replacement_text,
    )


def replace_detected_watermark(
    video_path: Path,
    *,
    replacement_text: str,
    output_path: Path | None = None,
    sample_count: int = 3,
) -> WatermarkReplacementResult:
    """Detect and replace a foreign watermark, otherwise leave the video untouched."""
    normalized_replacement = _normalize_handle(replacement_text)
    if not normalized_replacement:
        return WatermarkReplacementResult(
            output_path=None,
            region=None,
            replacement_text=replacement_text,
            skipped_reason="missing replacement handle",
        )

    region = detect_watermark(video_path, sample_count=sample_count)
    if region is None:
        return WatermarkReplacementResult(
            output_path=None,
            region=None,
            replacement_text=replacement_text,
            skipped_reason="no watermark detected",
        )

    if _same_or_prefix_handle(region.text, normalized_replacement):
        return WatermarkReplacementResult(
            output_path=None,
            region=region,
            replacement_text=replacement_text,
            skipped_reason="own watermark already present",
        )
    if len(_handle_body(region.text)) < _MIN_HANDLE_BODY_CHARS:
        return WatermarkReplacementResult(
            output_path=None,
            region=region,
            replacement_text=replacement_text,
            skipped_reason="weak watermark detection",
        )

    result = cover_watermark(
        video_path,
        region,
        replacement_text=normalized_replacement,
        output_path=output_path,
    )
    return WatermarkReplacementResult(
        output_path=result.output_path,
        region=result.region,
        replacement_text=normalized_replacement,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cover_geometry(
    region: WatermarkRegion,
    *,
    replacement_text: str | None,
    probe: VideoProbe | None,
) -> _WatermarkCoverGeometry:
    """Return cover geometry sized to match the detected watermark.

    Font size is derived from the detected region height so the replacement
    text renders at roughly the same scale as the original watermark.

    Long-username handling
    ----------------------
    The maximum box width is capped at 45% of the frame width.  If the
    replacement text would exceed that cap at the natural font size, the font
    size is scaled down proportionally so the text always fits in one line
    without overflow.
    """
    frame_width = probe.width if probe is not None else 720
    frame_height = probe.height if probe is not None else 1280

    # Font size derived from the detected watermark height.
    # Arial Black glyphs are roughly 0.72× font_size wide (measured empirically;
    # an earlier 0.65 estimate caused the badge to clip on long handles).
    base_font_size = max(18, min(42, int(region.h * 0.80)))
    _GLYPH_WIDTH_FACTOR = 0.72

    # Pill padding: roomy enough that the glyphs never touch the rounded
    # edges — a cramped badge reads as broken text at reel resolution.
    h_pad = max(8, base_font_size // 3)
    v_pad = max(4, base_font_size // 6)

    # Hard cap: 45% of frame width so long names never run off-screen.
    max_lane_width = int(frame_width * 0.45)
    char_count = len(replacement_text) if replacement_text else 1

    # Scale font down if text would exceed the cap at base size.
    available_for_text = max_lane_width - h_pad * 2
    estimated_at_base = int(char_count * base_font_size * _GLYPH_WIDTH_FACTOR)
    if estimated_at_base > available_for_text:
        font_size = max(14, int(base_font_size * available_for_text / estimated_at_base))
    else:
        font_size = base_font_size

    text_w = int(char_count * font_size * _GLYPH_WIDTH_FACTOR) if replacement_text else 0

    # IMPORTANT — Tesseract's bounding box wraps only the glyph centers; it
    # does NOT include the drop shadow, outline, or anti-aliased halo that
    # most Instagram/TikTok watermarks have. The actual visible watermark is
    # ~30% wider and ~30% taller than what we detected. We inflate the cover
    # region by these factors so the badge fully buries the original.
    _SHADOW_INFLATE_W = 1.30
    _SHADOW_INFLATE_H = 1.30
    region_cover_w = int(region.w * _SHADOW_INFLATE_W)
    region_cover_h = int(region.h * _SHADOW_INFLATE_H)

    # Width: must cover BOTH the rendered text AND the inflated original.
    lane_width = min(max_lane_width, max(text_w + h_pad * 2, region_cover_w + h_pad))
    # Height: must cover BOTH the rendered font AND the inflated original.
    lane_height = max(font_size + v_pad * 2, region_cover_h + v_pad)

    # Shift the badge so the inflated cover region is centred on the
    # detected watermark, not anchored to its top-left corner. Without this
    # the extra inflate would extend only down-right and miss the shadow/halo
    # that typically sits to the left and above the detected glyphs.
    horizontal_shift = max(0, (region_cover_w - region.w) // 2)
    vertical_shift = max(0, (region_cover_h - region.h) // 2)

    x = max(0, region.x - h_pad - horizontal_shift)
    y = max(0, region.y - v_pad - vertical_shift)
    x = min(x, max(0, frame_width - lane_width))
    y = min(y, max(0, frame_height - lane_height))

    # Pill-shaped corners: radius is half the box height so short handles look
    # like a pill badge; capped at 12 so tall boxes don't get overly rounded.
    corner_radius = min(lane_height // 2, 12)

    # Centre the text horizontally inside the (possibly inflated) lane,
    # so when the badge is wider than the text the extra space sits evenly
    # on both sides instead of all on the right.
    text_x = x + max(h_pad, (lane_width - text_w) // 2)

    return _WatermarkCoverGeometry(
        x=x,
        y=y,
        w=lane_width,
        h=lane_height,
        text_x=text_x,
        text_y=y + max(0, (lane_height - font_size) // 2) - 1,
        font_size=font_size,
        corner_radius=corner_radius,
    )


def _best_watermark_detection(detections: list[WatermarkRegion]) -> WatermarkRegion | None:
    candidates = [
        detection
        for detection in detections
        if len(_handle_body(detection.text)) >= _MIN_HANDLE_BODY_CHARS
    ]
    if not candidates:
        return None

    grouped: Counter[tuple[str, str]] = Counter(
        (_handle_body(detection.text), detection.corner) for detection in candidates
    )
    best_key = grouped.most_common(1)[0][0]
    for detection in candidates:
        if (_handle_body(detection.text), detection.corner) == best_key:
            return detection
    return candidates[0]


def _normalize_handle(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("@"):
        cleaned = "@" + cleaned
    match = _HANDLE_RE.fullmatch(cleaned)
    return match.group(0) if match else ""


def _handle_body(value: str) -> str:
    return value.strip().lstrip("@").casefold().replace(".", "").replace("_", "")


def _same_or_prefix_handle(detected: str, replacement: str) -> bool:
    detected_body = _handle_body(detected)
    replacement_body = _handle_body(replacement)
    if not detected_body or not replacement_body:
        return False
    return (
        detected_body == replacement_body
        or detected_body.startswith(replacement_body[: max(4, len(detected_body))])
        or replacement_body.startswith(detected_body)
    )


def _ocr_pass(
    ffmpeg_path: Path,
    tesseract_path: Path,
    video_path: Path,
    timestamps: list[float],
    scan_regions: list[tuple[str, int, int, int, int]],
    tmp_root: Path,
    *,
    enhance: bool,
    label: str,
) -> list[WatermarkRegion]:
    """Run one round of frame-extract + OCR across all timestamps and regions.

    Args:
        enhance: When True, applies contrast boost + greyscale + 2x upscale
                 to the extracted crop before handing it to Tesseract.
        label:   Short string used to name temp files so passes don't collide.

    Returns a (possibly empty) list of detected WatermarkRegion objects.
    """
    detections: list[WatermarkRegion] = []
    for idx, ts in enumerate(timestamps):
        for region_name, x_off, y_off, width, height in scan_regions:
            frame_path = tmp_root / f"f{idx}-{region_name}-{label}.png"
            try:
                _extract_region_crop(
                    ffmpeg_path,
                    video_path,
                    ts,
                    x=x_off,
                    y=y_off,
                    width=width,
                    height=height,
                    output_path=frame_path,
                    enhance=enhance,
                )
            except Exception:  # noqa: BLE001
                continue

            try:
                tsv = _run_tesseract_tsv(tesseract_path, frame_path)
                boxes = _parse_tesseract_text_boxes(tsv)
            except Exception:  # noqa: BLE001
                continue

            # Enhanced pass scales the image 2x so Tesseract coordinates are in
            # 2x space; divide by 2 before adding the region offset.
            coord_scale = 2.0 if enhance else 1.0
            region = _find_handle_in_boxes(
                boxes, x_off, y_off, region_name, coord_scale=coord_scale
            )
            if region is not None:
                detections.append(region)

    return detections


def _corner_dimensions(probe: VideoProbe) -> tuple[int, int]:
    """Return (corner_width, corner_height) clamped to actual video dimensions."""
    cw = max(_MIN_CORNER_W, int(probe.width * _CORNER_WIDTH_RATIO))
    ch = max(_MIN_CORNER_H, int(probe.height * _CORNER_HEIGHT_RATIO))
    return min(cw, probe.width), min(ch, probe.height)


def _corner_offsets(probe: VideoProbe, corner: str) -> tuple[int, int]:
    """Return (x_offset, y_offset) of the corner crop in full-video coordinates."""
    cw, ch = _corner_dimensions(probe)
    x_off = 0 if "left" in corner else probe.width - cw
    y_off = 0 if "top" in corner else probe.height - ch
    return x_off, y_off


def _watermark_scan_regions(probe: VideoProbe) -> list[tuple[str, int, int, int, int]]:
    """Return (name, x, y, w, h) regions to OCR for watermark handles.

    Coverage map (vertical bands, left side):
      0-20%   → top-left corner
      18-48%  → left-upper-mid  ← plugs the gap where @clips-style watermarks sit
                                   (just below a title band, upper-left of content)
      62-92%  → left-lower
      80-100% → bottom-left corner

    Right side has right-upper (15-50%) and right-mid (20-55%).
    """
    width = probe.width
    height = probe.height
    cw, ch = _corner_dimensions(probe)
    return [
        ("top-left", 0, 0, cw, ch),
        ("top-right", width - cw, 0, cw, ch),
        ("bottom-left", 0, height - ch, cw, ch),
        ("bottom-right", width - cw, height - ch, cw, ch),
        # Left mid: catches watermarks just below a title band (e.g. @clips style).
        # Covers y=18%-48%, left 40% of frame — the gap between top-left and left-lower.
        ("left-upper-mid", 0, int(height * 0.18), int(width * 0.40), int(height * 0.30)),
        ("right-upper", int(width * 0.45), int(height * 0.15), int(width * 0.55), int(height * 0.35)),
        ("right-mid", int(width * 0.55), int(height * 0.20), int(width * 0.45), int(height * 0.35)),
        ("left-lower", 0, int(height * 0.62), int(width * 0.58), int(height * 0.30)),
        ("bottom-center", int(width * 0.25), int(height * 0.68), int(width * 0.50), int(height * 0.25)),
    ]


def _extract_corner_crop(
    ffmpeg_path: Path,
    input_path: Path,
    timestamp: float,
    probe: VideoProbe,
    corner: str,
    output_path: Path,
) -> None:
    """Extract a corner region from a single frame as a PNG."""
    cw, ch = _corner_dimensions(probe)
    x_off, y_off = _corner_offsets(probe, corner)
    command = [
        str(ffmpeg_path),
        "-hide_banner", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", str(input_path),
        "-frames:v", "1",
        "-vf", f"crop={cw}:{ch}:{x_off}:{y_off}",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )


def _extract_region_crop(
    ffmpeg_path: Path,
    input_path: Path,
    timestamp: float,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    output_path: Path,
    enhance: bool = False,
) -> None:
    """Extract a rectangular crop from a single frame as a PNG.

    When ``enhance`` is True the crop is post-processed with:
    - ``negate`` — inverts the image so white-on-light watermarks (the most
      common Instagram/TikTok style: white bold text, dark outline, on a sky
      or content background) become dark on a medium-grey background.
    - ``eq=brightness=0.3:saturation=0`` — lifts mid-tones so the background
      rises to a light grey (~150) while the text interior (near 0 after
      negate) stays dark; greyscale removes colour noise.
    - ``scale=iw*2:ih*2`` — doubles pixel dimensions so Tesseract receives
      larger glyphs, improving recognition of small or blended text.

    This chain is the complement of simple contrast boost: contrast boost
    helps dark-background watermarks; negate+brightness lift helps
    light-background watermarks (white text on sky/content).
    """
    crop_filter = f"crop={width}:{height}:{x}:{y}"
    if enhance:
        vf = f"{crop_filter},negate,eq=brightness=0.3:saturation=0,scale=iw*2:ih*2"
    else:
        vf = crop_filter

    command = [
        str(ffmpeg_path),
        "-hide_banner", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", str(input_path),
        "-frames:v", "1",
        "-vf", vf,
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )


def _find_handle_in_boxes(
    boxes: list[OcrTextBox],
    x_offset: int,
    y_offset: int,
    corner: str,
    *,
    coord_scale: float = 1.0,
) -> WatermarkRegion | None:
    """Search OCR text boxes for an @handle. Returns region in full-video coords.

    Two passes:
    1. Look for a single box whose raw text contains a full @handle match.
    2. Join adjacent boxes on the same line (Tesseract sometimes splits
       ``@`` from ``username``) and search the joined text.

    Args:
        coord_scale: Divide all Tesseract box coordinates by this factor before
            adding the region offset. Use ``2.0`` when the image fed to Tesseract
            was scaled up 2x (as in the enhanced pass) so the returned region
            lands in original-video pixel space, not scaled-image space.
    """
    if not boxes:
        return None

    def _to_video(px: int) -> int:
        return int(px / coord_scale)

    # Pass 1: single-box match
    for box in boxes:
        if box.confidence < _MIN_CONFIDENCE:
            continue
        m = _HANDLE_RE.search(box.text)
        if m:
            return WatermarkRegion(
                x=_to_video(box.left) + x_offset,
                y=_to_video(box.top) + y_offset,
                w=_to_video(box.width),
                h=_to_video(box.height),
                text=m.group(0),
                corner=corner,
            )

    # Pass 2: reconstruct by joining boxes on the same line
    for group in _group_boxes_by_line(boxes):
        joined = "".join(b.text for b in group)
        m = _HANDLE_RE.search(joined)
        if m:
            x = _to_video(min(b.left for b in group)) + x_offset
            y = _to_video(min(b.top for b in group)) + y_offset
            right = _to_video(max(b.left + b.width for b in group))
            bottom = _to_video(max(b.top + b.height for b in group))
            return WatermarkRegion(
                x=x,
                y=y,
                w=right - (x - x_offset),
                h=bottom - (y - y_offset),
                text=m.group(0),
                corner=corner,
            )

    return None


def _group_boxes_by_line(
    boxes: list[OcrTextBox],
    *,
    v_tolerance: int = 8,
    h_gap_max: int = 30,
) -> list[list[OcrTextBox]]:
    """Group OCR boxes into horizontal lines.

    Boxes are on the same line if their tops are within ``v_tolerance`` pixels
    and the horizontal gap between consecutive boxes is within ``h_gap_max``.
    """
    if not boxes:
        return []
    sorted_boxes = sorted(boxes, key=lambda b: (b.top, b.left))
    groups: list[list[OcrTextBox]] = [[sorted_boxes[0]]]
    for box in sorted_boxes[1:]:
        last_group = groups[-1]
        last = last_group[-1]
        same_line = abs(box.top - last.top) <= v_tolerance
        close_enough = (box.left - (last.left + last.width)) <= h_gap_max
        if same_line and close_enough:
            last_group.append(box)
        else:
            groups.append([box])
    return groups


def _rounded_rect_filter(
    x: int, y: int, w: int, h: int,
    *,
    radius: int = 10,
    opacity: float = 0.72,
) -> str:
    """Return a geq filter that darkens a rounded-rectangle region.

    Uses per-pixel distance math so corners are genuinely rounded, not square.
    ``opacity`` controls how much the background is darkened (0 = no change,
    1 = fully black).  The region outside the rounded rect is untouched.

    The distance formula:  a pixel (X, Y) is inside the rounded rectangle if
    its distance to the nearest point on the inner axis-aligned rect
    (shrunken by ``radius`` on all sides) is ≤ ``radius``.  This gives the
    standard CSS/SVG border-radius behaviour.
    """
    keep = round(1.0 - opacity, 4)   # fraction of original brightness to keep

    cx = (x + x + w) / 2.0           # centre x
    cy = (y + y + h) / 2.0           # centre y
    rw = max(0.0, w / 2.0 - radius)  # inner half-width
    rh = max(0.0, h / 2.0 - radius)  # inner half-height

    # FFmpeg's simple filterchain parser splits on ALL commas, even those inside
    # parentheses. Every comma inside a geq expression must be escaped as \,
    # so the option-value parser passes it through to the expression evaluator.
    def _fc(s: str) -> str:
        """Escape commas for FFmpeg filter option values."""
        return s.replace(",", "\\,")

    dist = (
        f"sqrt(pow(max(0{_fc(',')}abs(X-{cx})-{rw}){_fc(',')}2)"
        f"+pow(max(0{_fc(',')}abs(Y-{cy})-{rh}){_fc(',')}2))"
    )
    inside = f"lte({dist}{_fc(',')}{radius})"

    # p(X,Y) returns the current channel's value — works for r=, g= and b=.
    pxy = f"p(X{_fc(',')}Y)"
    channel_expr = f"if({inside}{_fc(',')}{keep}*{pxy}{_fc(',')}{pxy})"

    return f"geq=r={channel_expr}:g={channel_expr}:b={channel_expr}"


def _font_arg() -> str:
    """Return FFmpeg fontfile= prefix string, or empty string if unavailable."""
    font = windows_font_file()
    if font is not None:
        safe = str(font).replace("\\", "/").replace(":", "\\:")
        return f"fontfile='{safe}':"
    return ""
