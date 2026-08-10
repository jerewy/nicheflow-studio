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
from concurrent.futures import ThreadPoolExecutor
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

# Scan tiles step by half a tile, so any handle narrower than half a tile is
# fully inside at least one tile no matter where it sits.
_TILE_STEP_RATIO = 0.5

# Concurrency for the OCR stage. Each Tesseract call is a short-lived
# subprocess, so threads scale on it (the GIL is released while it runs) — but
# they are CPU-bound processes, so this is capped well under the core count to
# leave the machine responsive during a long batch.
_MAX_OCR_WORKERS = 8

# FFmpeg's `eq=brightness=0.3` shifts luma by 0.3 of full range; 8-bit equivalent.
_ENHANCE_BRIGHTNESS_LIFT = round(0.3 * 255)


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

    The detection samples ``sample_count`` evenly-spaced frames and OCRs
    overlapping tiles across the footage (see :func:`_watermark_scan_regions`).
    The most consistently detected handle wins.

    Two-pass strategy
    -----------------
    Pass 1 (raw): OCR on the unmodified crop — works for normal solid watermarks.
    Pass 2 (enhanced): the same tiles with a negated, greyscale, 2x-upscaled
    crop, which makes semi-transparent or low-contrast watermarks (white text on
    sky/content) readable. It also samples more timestamps, so a mark that is
    off-screen during a scene cut still gets seen.

    Both passes ALWAYS run and their detections are pooled. Pass 2 used to be a
    fallback that fired only when pass 1 came up empty, on the reasoning that it
    was expensive — but the two passes read the same mark to different depths,
    and stopping at pass 1's shallower read (``@HISTORYBY`` vs the full
    ``@HISTORYBYPASS``) sized the cover too small and left the tail of the
    original visible. Since a pass now costs one FFmpeg seek per timestamp plus
    concurrent OCR, paying for both is cheaper than the old single fallback pass.
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

    # Scope the scan to the embedded footage when it can be found: the foreign
    # watermark is always inside it, and on an exported reel everything outside
    # it is text this app drew (post header, title band) — OCR there produces
    # nothing but false positives.
    scan_regions = _watermark_scan_regions(
        probe, footage_rect=_footage_rect(resolved, probe)
    )

    with tempfile.TemporaryDirectory(prefix="nicheflow-wm-") as tmp:
        tmp_root = Path(tmp)

        # Pass 1: raw crop — fast path, handles normal solid-contrast watermarks.
        detections = _ocr_pass(
            ffmpeg_path, tesseract_path, resolved,
            timestamps, scan_regions, tmp_root,
            enhance=False, label="raw",
        )

        # Pass 2: enhanced crop. Negate + brightness-lift makes light-on-light
        # marks readable, and the extra timestamps reduce the chance of every
        # sample landing where the mark is off-screen (animated intros, cuts).
        # Pooled with pass 1 rather than replacing it: whichever pass read the
        # mark most completely is what _best_watermark_detection sizes on.
        detections += _ocr_pass(
            ffmpeg_path, tesseract_path, resolved,
            _sample_timestamps(probe, count=max(5, sample_count * 2)),
            scan_regions, tmp_root,
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


def _boxes_overlap(a: WatermarkRegion, b: WatermarkRegion, *, slack: int) -> bool:
    """Whether two detected boxes are the same on-screen mark, within ``slack``."""
    return not (
        a.x > b.x + b.w + slack
        or b.x > a.x + a.w + slack
        or a.y > b.y + b.h + slack
        or b.y > a.y + a.h + slack
    )


def _best_watermark_detection(detections: list[WatermarkRegion]) -> WatermarkRegion | None:
    """The most consistently detected handle, as a box covering its full extent.

    Three things this has to get right, each learned from a real miss:

    1. **Votes count per handle BODY, not per region.** Scan tiles overlap by
       design, so one real watermark is normally found in two to four adjacent
       tiles. Keying the count on the tile name split those votes and let a
       single spurious read outrank a mark seen repeatedly.
    2. **A truncated read reinforces the fullest one instead of competing with
       it.** OCR reads the same mark to different depths — ``@HISTORYBY`` from
       the raw pass, ``@HISTORYBYPASS`` from the enhanced one. Treated as rival
       handles, the short read could win and the cover, sized to its box, left
       the tail of the original showing.
    3. **The returned box is the union of that mark's reads.** Same reason: the
       widest read is the best estimate of what actually has to be buried.
       Only boxes that overlap the fullest read are merged, so a handle that
       genuinely appears twice in different places never yields one giant rect
       spanning both.
    """
    candidates = [
        detection
        for detection in detections
        if len(_handle_body(detection.text)) >= _MIN_HANDLE_BODY_CHARS
    ]
    if not candidates:
        return None

    bodies = {_handle_body(detection.text) for detection in candidates}

    def _fullest(body: str) -> str:
        """The longest body this one is a prefix of (or itself)."""
        return max(
            (other for other in bodies if other.startswith(body) or body.startswith(other)),
            key=len,
        )

    votes: Counter[str] = Counter(_fullest(_handle_body(d.text)) for d in candidates)
    best_body = votes.most_common(1)[0][0]
    group = [d for d in candidates if _fullest(_handle_body(d.text)) == best_body]

    # Seed on the longest read: it saw the most of the mark, so its box is the
    # best anchor and its text the most complete.
    seed = max(group, key=lambda d: (len(_handle_body(d.text)), d.w))
    slack = max(4, seed.h // 2)
    merged = [d for d in group if _boxes_overlap(d, seed, slack=slack)]
    left = min(d.x for d in merged)
    top = min(d.y for d in merged)
    return WatermarkRegion(
        x=left,
        y=top,
        w=max(d.x + d.w for d in merged) - left,
        h=max(d.y + d.h for d in merged) - top,
        text=seed.text,
        corner=seed.corner,
    )


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

    Returns a (possibly empty) list of detected WatermarkRegion objects, in
    timestamp-then-region order regardless of the order the OCR calls finished,
    so :func:`_best_watermark_detection` stays deterministic.

    Cost shape: ONE FFmpeg seek per timestamp (not one per region — the regions
    are cut out of that single decoded frame in-process), then the per-region
    Tesseract calls run concurrently. Before that, a 9-region scan spent 9
    FFmpeg launches re-decoding the same frame and ran every OCR back to back,
    which is what made this step ~40s per reel.
    """
    coord_scale = 2.0 if enhance else 1.0
    detections: list[WatermarkRegion] = []
    for idx, ts in enumerate(timestamps):
        frame_path = tmp_root / f"frame{idx}-{label}.png"
        try:
            _extract_full_frame(ffmpeg_path, video_path, ts, frame_path)
            crops = _write_region_crops(
                frame_path, scan_regions, tmp_root, index=idx, label=label, enhance=enhance
            )
        except Exception:  # noqa: BLE001 - a bad frame just contributes nothing
            continue

        def _ocr(item: tuple[tuple[str, int, int, int, int], Path]):
            (region_name, x_off, y_off, _w, _h), crop_path = item
            try:
                boxes = _parse_tesseract_text_boxes(
                    _run_tesseract_tsv(tesseract_path, crop_path)
                )
            except Exception:  # noqa: BLE001
                return None
            return _find_handle_in_boxes(
                boxes, x_off, y_off, region_name, coord_scale=coord_scale
            )

        workers = max(1, min(_MAX_OCR_WORKERS, len(crops)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # map() keeps results in submission order, which is what makes the
            # detection list independent of thread scheduling.
            for region in pool.map(_ocr, crops):
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


def _tile_origins(start: int, span: int, tile: int, step: int) -> list[int]:
    """Tile origins covering ``start..start+span``, always flush to both ends.

    The last origin is pinned to the far edge rather than left wherever the
    stride happened to stop, so the trailing strip is never unscanned — that
    off-by-a-stride strip is exactly where a bottom-right watermark hides.
    """
    if span <= tile:
        return [start]
    origins = list(range(start, start + span - tile + 1, step))
    last = start + span - tile
    if not origins or origins[-1] != last:
        origins.append(last)
    return origins


def _watermark_scan_regions(
    probe: VideoProbe,
    *,
    footage_rect: tuple[int, int, int, int] | None = None,
) -> list[tuple[str, int, int, int, int]]:
    """Return (name, x, y, w, h) regions to OCR for watermark handles.

    Tiles the search area with corner-sized crops stepping by half a tile, so
    every pixel is covered and a handle straddling one tile boundary is still
    whole inside its neighbour. This replaced nine hand-placed "lanes" that left
    gaps between them: a real ``@historybypass`` mark at the bottom-right of the
    footage sat in the gap below ``right-mid`` and above ``bottom-center`` and
    was never OCR'd at all, while the lanes DID reach up into the app's own
    burned-in post header and produced false positives there.

    ``footage_rect`` (x, y, w, h) restricts tiling to the embedded footage. Two
    reasons: a foreign watermark is always inside the footage, and everything
    outside it on an exported reel is text this app drew itself — scanning our
    own header/title can only ever yield a false positive. Falls back to the
    whole frame when the footage rectangle is unknown (e.g. a raw clip whose
    footage already fills the canvas).
    """
    tile_w, tile_h = _corner_dimensions(probe)
    if footage_rect is not None:
        x0, y0, span_w, span_h = footage_rect
    else:
        x0, y0, span_w, span_h = 0, 0, probe.width, probe.height
    tile_w = min(tile_w, span_w)
    tile_h = min(tile_h, span_h)
    step_x = max(1, int(tile_w * _TILE_STEP_RATIO))
    step_y = max(1, int(tile_h * _TILE_STEP_RATIO))

    regions: list[tuple[str, int, int, int, int]] = []
    for row, y in enumerate(_tile_origins(y0, span_h, tile_h, step_y)):
        for col, x in enumerate(_tile_origins(x0, span_w, tile_w, step_x)):
            regions.append((f"r{row}c{col}", x, y, tile_w, tile_h))
    return regions


def _footage_rect(
    video_path: Path, probe: VideoProbe
) -> tuple[int, int, int, int] | None:
    """The embedded footage rectangle as (x, y, w, h), or None if not found.

    Best-effort: a failure just means the scan tiles the whole frame, which is
    correct but slower and re-exposes the app's own overlays to OCR.
    """
    try:
        # Local import: video imports nothing from here, but keeping the heavy
        # numpy-backed detector out of module import stays consistent with how
        # the rest of this module reaches into video.
        from nicheflow_studio.processing.video import detect_content_rectangle

        crop = detect_content_rectangle(video_path, probe)
    except Exception:  # noqa: BLE001 - detection is advisory
        return None
    if crop is None:
        return None
    width = probe.width - crop.left - crop.right
    height = probe.height - crop.top - crop.bottom
    if width < _MIN_CORNER_W or height < _MIN_CORNER_H:
        return None
    return crop.left, crop.top, width, height


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


def _extract_full_frame(
    ffmpeg_path: Path,
    input_path: Path,
    timestamp: float,
    output_path: Path,
) -> None:
    """Extract one whole frame as a PNG.

    The scan cuts every region out of this one decode. Cropping per region in
    FFmpeg instead meant N seeks and N decodes of the same frame for N regions.
    """
    command = [
        str(ffmpeg_path),
        "-hide_banner", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", str(input_path),
        "-frames:v", "1",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )


def _enhance_crop(image):  # noqa: ANN001, ANN201 - PIL image in, PIL image out
    """Pillow equivalent of the FFmpeg enhance chain used by the second pass.

    Mirrors ``negate,eq=brightness=0.3:saturation=0,scale=iw*2:ih*2``:
    invert, then greyscale (saturation=0) with mid-tones lifted by 0.3 of full
    range (~77/255), then 2x upscale. Kept in lockstep with
    :func:`_extract_region_crop`, which remains the FFmpeg reference for it.
    """
    from PIL import Image, ImageOps

    inverted = ImageOps.invert(image.convert("RGB")).convert("L")
    lifted = inverted.point(lambda value: min(255, value + _ENHANCE_BRIGHTNESS_LIFT))
    return lifted.resize(
        (lifted.width * 2, lifted.height * 2), resample=Image.BICUBIC
    )


def _write_region_crops(
    frame_path: Path,
    scan_regions: list[tuple[str, int, int, int, int]],
    tmp_root: Path,
    *,
    index: int,
    label: str,
    enhance: bool,
) -> list[tuple[tuple[str, int, int, int, int], Path]]:
    """Cut every scan region out of one extracted frame. Returns (region, path)."""
    from PIL import Image

    crops: list[tuple[tuple[str, int, int, int, int], Path]] = []
    with Image.open(frame_path) as frame:
        frame.load()
        for region in scan_regions:
            name, x, y, width, height = region
            box = (x, y, min(x + width, frame.width), min(y + height, frame.height))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            crop = frame.crop(box)
            if enhance:
                crop = _enhance_crop(crop)
            crop_path = tmp_root / f"f{index}-{name}-{label}.png"
            crop.save(crop_path)
            crops.append((region, crop_path))
    return crops


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

    The FFmpeg reference for the enhance chain that :func:`_enhance_crop`
    mirrors in Pillow. The scan itself no longer calls this (one decode per
    region was most of its cost); kept because it is the ground truth those
    two implementations are checked against.

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

    Each group is returned in left-to-right order, NOT in the ``(top, left)``
    order used to walk the boxes. Those differ whenever two boxes on one line
    sit a pixel or two apart vertically, and the caller joins the group into a
    string — so returning sort order let a box further right be prepended and
    fabricate text that is nowhere on screen. That is how a "Vaulted History"
    post header plus a verified badge misread as ``@`` became the handle
    ``@story`` and got covered as if it were a foreign watermark.
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
    return [sorted(group, key=lambda b: b.left) for group in groups]


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
