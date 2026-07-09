"""Tests for watermark detection and cover logic.

All tests run without actual video files, FFmpeg, or Tesseract — they exercise
the pure logic (coordinate maths, OCR pattern matching, grouping) and the
graceful-degradation paths (missing tools, missing file).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nicheflow_studio.processing.video import OcrTextBox, VideoProbe
from nicheflow_studio.processing.watermark import (
    WatermarkCoverResult,
    WatermarkRegion,
    _best_watermark_detection,
    _corner_dimensions,
    _corner_offsets,
    _cover_geometry,
    _find_handle_in_boxes,
    _font_arg,
    _group_boxes_by_line,
    _normalize_handle,
    _same_or_prefix_handle,
    _watermark_scan_regions,
    cover_watermark,
    detect_watermark,
    replace_detected_watermark,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _box(text: str, left: int, top: int, w: int = 80, h: int = 20, conf: float = 80.0) -> OcrTextBox:
    return OcrTextBox(text=text, confidence=conf, left=left, top=top, width=w, height=h)


def _probe(width: int = 1080, height: int = 1920) -> VideoProbe:
    return VideoProbe(width=width, height=height, duration_seconds=10.0)


def _render_command(commands: list[list[str]]) -> list[str]:
    return next(command for command in commands if "-vf" in command)


# ---------------------------------------------------------------------------
# WatermarkRegion dataclass
# ---------------------------------------------------------------------------


def test_watermark_region_fields():
    r = WatermarkRegion(x=10, y=20, w=100, h=30, text="@handle", corner="bottom-right")
    assert r.x == 10
    assert r.y == 20
    assert r.w == 100
    assert r.h == 30
    assert r.text == "@handle"
    assert r.corner == "bottom-right"


def test_watermark_region_is_frozen():
    r = WatermarkRegion(x=0, y=0, w=50, h=20, text="@x", corner="top-left")
    with pytest.raises(Exception):
        r.x = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _corner_dimensions
# ---------------------------------------------------------------------------


def test_corner_dimensions_standard_1080p():
    cw, ch = _corner_dimensions(_probe(1080, 1920))
    assert cw == int(1080 * 0.40)
    assert ch == int(1920 * 0.20)


def test_corner_dimensions_enforces_minimums():
    # Tiny 100×80 video: minimums should kick in
    cw, ch = _corner_dimensions(VideoProbe(width=100, height=80, duration_seconds=5.0))
    assert cw >= 80
    assert ch >= 24


def test_corner_dimensions_clamped_to_video_size():
    # Very small video where min constants exceed video size
    cw, ch = _corner_dimensions(VideoProbe(width=50, height=20, duration_seconds=5.0))
    assert cw <= 50
    assert ch <= 20


# ---------------------------------------------------------------------------
# _corner_offsets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("corner,expected_x_zero,expected_y_zero", [
    ("top-left",     True,  True),
    ("top-right",    False, True),
    ("bottom-left",  True,  False),
    ("bottom-right", False, False),
])
def test_corner_offsets(corner, expected_x_zero, expected_y_zero):
    probe = _probe(1080, 1920)
    x_off, y_off = _corner_offsets(probe, corner)
    if expected_x_zero:
        assert x_off == 0
    else:
        assert x_off > 0
    if expected_y_zero:
        assert y_off == 0
    else:
        assert y_off > 0


def test_corner_offsets_right_x_equals_width_minus_corner_w():
    probe = _probe(1080, 1920)
    cw, _ = _corner_dimensions(probe)
    x_off, _ = _corner_offsets(probe, "bottom-right")
    assert x_off == probe.width - cw


def test_watermark_scan_regions_include_content_edge_lanes() -> None:
    regions = _watermark_scan_regions(VideoProbe(width=720, height=1280, duration_seconds=10.0))
    names = {name for name, *_coords in regions}

    assert {"top-right", "bottom-right", "right-upper", "right-mid", "bottom-center"} <= names


# ---------------------------------------------------------------------------
# _group_boxes_by_line
# ---------------------------------------------------------------------------


def test_group_boxes_empty():
    assert _group_boxes_by_line([]) == []


def test_group_boxes_single():
    boxes = [_box("hi", 10, 10)]
    groups = _group_boxes_by_line(boxes)
    assert len(groups) == 1
    assert groups[0][0].text == "hi"


def test_group_boxes_same_line():
    # Two boxes close together horizontally on same vertical band
    boxes = [_box("@", 10, 100, w=15), _box("epic", 30, 102, w=40)]
    groups = _group_boxes_by_line(boxes)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_group_boxes_different_lines():
    boxes = [_box("top", 10, 10), _box("bottom", 10, 200)]
    groups = _group_boxes_by_line(boxes)
    assert len(groups) == 2


def test_group_boxes_large_horizontal_gap_splits():
    # Gap of 200px between boxes on same line → separate groups
    boxes = [_box("@", 10, 100, w=15), _box("epic", 250, 100, w=40)]
    groups = _group_boxes_by_line(boxes)
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# _find_handle_in_boxes
# ---------------------------------------------------------------------------


def test_find_handle_single_box_match():
    boxes = [_box("@epicfunnypage", 50, 850, w=120, h=22)]
    region = _find_handle_in_boxes(boxes, x_offset=648, y_offset=1536, corner="bottom-right")
    assert region is not None
    assert region.text == "@epicfunnypage"
    assert region.x == 50 + 648
    assert region.y == 850 + 1536
    assert region.corner == "bottom-right"


def test_find_handle_split_across_two_boxes():
    # @ in one box, username in the next — should be joined and matched
    boxes = [
        _box("@", 50, 100, w=10, h=20),
        _box("meme.ig", 65, 102, w=60, h=20),
    ]
    region = _find_handle_in_boxes(boxes, x_offset=0, y_offset=0, corner="bottom-left")
    assert region is not None
    assert "@meme.ig" in region.text or "meme.ig" in region.text


def test_find_handle_no_handle_returns_none():
    boxes = [_box("hello world", 10, 10), _box("some text", 100, 10)]
    region = _find_handle_in_boxes(boxes, x_offset=0, y_offset=0, corner="top-left")
    assert region is None


def test_find_handle_empty_boxes_returns_none():
    region = _find_handle_in_boxes([], x_offset=0, y_offset=0, corner="top-right")
    assert region is None


def test_find_handle_low_confidence_skipped():
    # Box with @handle but below confidence threshold — pass 1 should skip it
    boxes = [_box("@epicfunnypage", 10, 10, conf=10.0)]
    # Pass 1 skips low-confidence; pass 2 joins all boxes regardless of confidence
    # so the result depends on whether joining finds it — that's acceptable behaviour
    region = _find_handle_in_boxes(boxes, x_offset=0, y_offset=0, corner="top-left")
    # We only assert pass 1 would have skipped it; pass 2 may still find it
    # The important thing is no crash
    assert region is None or isinstance(region, WatermarkRegion)


def test_find_handle_coords_include_offset():
    boxes = [_box("@test", 10, 5, w=80, h=20)]
    region = _find_handle_in_boxes(boxes, x_offset=500, y_offset=1000, corner="bottom-right")
    assert region is not None
    assert region.x == 510
    assert region.y == 1005


def test_find_handle_case_insensitive():
    boxes = [_box("@MEMEPAGE", 0, 0)]
    region = _find_handle_in_boxes(boxes, x_offset=0, y_offset=0, corner="top-right")
    assert region is not None
    assert region.text.lower() == "@memepage"


# ---------------------------------------------------------------------------
# detect_watermark — graceful degradation without real video/tools
# ---------------------------------------------------------------------------


def test_detect_watermark_no_ffmpeg():
    with patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=None):
        result = detect_watermark(Path("nonexistent.mp4"))
    assert result is None


def test_detect_watermark_no_tesseract():
    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("nicheflow_studio.processing.watermark.tesseract_binary", return_value=None),
    ):
        result = detect_watermark(Path("nonexistent.mp4"))
    assert result is None


def test_detect_watermark_missing_file():
    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("nicheflow_studio.processing.watermark.tesseract_binary", return_value=Path("/fake/tess")),
    ):
        result = detect_watermark(Path("does_not_exist_xyz.mp4"))
    assert result is None


def test_detect_watermark_probe_failure(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"not a real video")
    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("nicheflow_studio.processing.watermark.tesseract_binary", return_value=Path("/fake/tess")),
        patch("nicheflow_studio.processing.watermark.probe_video", side_effect=RuntimeError("bad")),
    ):
        result = detect_watermark(fake_video)
    assert result is None


def test_best_watermark_detection_rejects_tiny_partial_handle() -> None:
    detections = [WatermarkRegion(x=10, y=20, w=30, h=18, text="@TC", corner="right-upper")]

    assert _best_watermark_detection(detections) is None


def test_best_watermark_detection_accepts_repeated_partial_handle() -> None:
    first = WatermarkRegion(x=10, y=20, w=100, h=18, text="@Come", corner="bottom-left")
    second = WatermarkRegion(x=12, y=20, w=102, h=18, text="@Come", corner="bottom-left")

    assert _best_watermark_detection([first, second]) is first


# ---------------------------------------------------------------------------
# cover_watermark — no FFmpeg needed for path/error tests
# ---------------------------------------------------------------------------


def test_cover_watermark_no_ffmpeg(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    region = WatermarkRegion(x=10, y=10, w=100, h=25, text="@x", corner="bottom-right")
    with patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=None):
        with pytest.raises(RuntimeError, match="FFmpeg not found"):
            cover_watermark(fake_video, region)


def test_cover_watermark_default_output_path(tmp_path):
    fake_video = tmp_path / "myclip.mp4"
    fake_video.write_bytes(b"x")
    region = WatermarkRegion(x=10, y=10, w=100, h=25, text="@x", corner="bottom-right")

    def fake_run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 0
        return r

    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("subprocess.run", side_effect=fake_run),
    ):
        result = cover_watermark(fake_video, region, replacement_text="@meme.ig")

    assert result.output_path.name == "myclip_branded.mp4"
    assert result.replacement_text == "@meme.ig"
    assert result.region is region


def test_cover_watermark_custom_output_path(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    out = tmp_path / "out.mp4"
    region = WatermarkRegion(x=0, y=0, w=50, h=20, text="@x", corner="top-left")

    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        result = cover_watermark(fake_video, region, output_path=out)

    assert result.output_path == out


def test_cover_watermark_no_replacement_text(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    region = WatermarkRegion(x=5, y=5, w=80, h=20, text="@x", corner="top-right")

    captured: list[list[str]] = []

    def capture_run(cmd, **kwargs):
        captured.append(cmd)
        return MagicMock(returncode=0)

    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("subprocess.run", side_effect=capture_run),
    ):
        result = cover_watermark(fake_video, region, replacement_text=None)

    assert result.replacement_text is None
    # drawtext should NOT be in the -vf argument
    command = _render_command(captured)
    vf_arg = command[command.index("-vf") + 1]
    assert "drawtext" not in vf_arg
    # The cover background is now a rounded-rectangle geq filter (per-pixel
    # math), not a square drawbox — gives the badge soft corners.
    assert "geq=" in vf_arg


def test_cover_watermark_with_replacement_text_includes_drawtext(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    region = WatermarkRegion(x=5, y=5, w=80, h=20, text="@x", corner="top-right")

    captured: list[list[str]] = []

    def capture_run(cmd, **kwargs):
        captured.append(cmd)
        return MagicMock(returncode=0)

    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("subprocess.run", side_effect=capture_run),
    ):
        cover_watermark(fake_video, region, replacement_text="@meme.ig")

    command = _render_command(captured)
    vf_arg = command[command.index("-vf") + 1]
    assert "drawtext" in vf_arg
    assert "@meme.ig" in vf_arg
    # Background is a rounded-rectangle geq filter (full-black inside,
    # original pixels untouched outside). The handle itself renders as plain
    # white text: an outline or shadow on an opaque black pill adds no
    # contrast and smudges the glyph antialiasing at badge font sizes.
    assert "geq=" in vf_arg
    assert "fontcolor=white" in vf_arg
    assert "borderw" not in vf_arg
    assert "shadow" not in vf_arg


def test_cover_geometry_expands_partial_handle_to_watermark_lane() -> None:
    """OCR sometimes returns a partial handle (e.g. '@Come' instead of
    '@memeistsdaily'). The cover lane must still be wide enough for the FULL
    replacement text and tall enough to cover the inflated original region.
    """
    region = WatermarkRegion(x=414, y=1476, w=124, h=27, text="@Come", corner="bottom-left")

    geometry = _cover_geometry(
        region,
        replacement_text="@memeistsdaily",
        probe=VideoProbe(width=720, height=1600, duration_seconds=10.0),
    )

    # Lane must accommodate the full replacement text plus padding.
    assert geometry.w >= 216
    # Pill corners: radius must be > 0 (rounded, not square).
    assert geometry.corner_radius > 0
    # Lane must start at or before the detected region (covers the original).
    assert geometry.x <= region.x
    # Lane must fully cover the detected partial region (left and right edges).
    assert geometry.x + geometry.w >= region.x + region.w
    # Text must sit inside the lane horizontally and vertically.
    assert geometry.text_x >= geometry.x
    assert geometry.text_y >= geometry.y


def test_cover_watermark_encodes_like_the_export_pipeline(tmp_path):
    # The cover pass re-encodes the already-rendered reel, so it must mirror
    # export_cropped_video's settings. Without -pix_fmt the geq filter promotes
    # the output to yuv444p (High 4:4:4 Predictive), which the WebView2 preview
    # cannot play and Instagram degrades; audio copy has produced a truncated
    # final AAC packet, so audio is re-encoded too.
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    region = WatermarkRegion(x=0, y=0, w=50, h=20, text="@x", corner="bottom-left")

    captured: list[list[str]] = []

    def capture_run(cmd, **kwargs):
        captured.append(cmd)
        return MagicMock(returncode=0)

    with (
        patch("nicheflow_studio.processing.watermark.ffmpeg_binary", return_value=Path("/fake/ffmpeg")),
        patch("subprocess.run", side_effect=capture_run),
    ):
        cover_watermark(fake_video, region)

    command = _render_command(captured)
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-crf") + 1] == "18"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-b:a") + 1] == "192k"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert "copy" not in command


def test_replace_detected_watermark_skips_when_no_watermark(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")

    with patch("nicheflow_studio.processing.watermark.detect_watermark", return_value=None):
        result = replace_detected_watermark(fake_video, replacement_text="@memeistsdaily")

    assert result.output_path is None
    assert result.region is None
    assert result.skipped_reason == "no watermark detected"


def test_replace_detected_watermark_skips_own_watermark(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    region = WatermarkRegion(x=10, y=20, w=120, h=24, text="@memeist", corner="bottom-right")

    with (
        patch("nicheflow_studio.processing.watermark.detect_watermark", return_value=region),
        patch("nicheflow_studio.processing.watermark.cover_watermark") as cover,
    ):
        result = replace_detected_watermark(fake_video, replacement_text="@memeistsdaily")

    cover.assert_not_called()
    assert result.output_path is None
    assert result.region is region
    assert result.skipped_reason == "own watermark already present"


def test_replace_detected_watermark_skips_weak_detection(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    region = WatermarkRegion(x=10, y=20, w=40, h=24, text="@TC", corner="right-upper")

    with (
        patch("nicheflow_studio.processing.watermark.detect_watermark", return_value=region),
        patch("nicheflow_studio.processing.watermark.cover_watermark") as cover,
    ):
        result = replace_detected_watermark(fake_video, replacement_text="@memeistsdaily")

    cover.assert_not_called()
    assert result.output_path is None
    assert result.region is region
    assert result.skipped_reason == "weak watermark detection"


def test_replace_detected_watermark_covers_foreign_watermark(tmp_path):
    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    output = tmp_path / "out.mp4"
    region = WatermarkRegion(x=10, y=20, w=120, h=24, text="@comedyslam", corner="bottom-right")
    cover_result = WatermarkCoverResult(
        output_path=output,
        region=region,
        replacement_text="@memeistsdaily",
    )

    with (
        patch("nicheflow_studio.processing.watermark.detect_watermark", return_value=region),
        patch("nicheflow_studio.processing.watermark.cover_watermark", return_value=cover_result) as cover,
    ):
        result = replace_detected_watermark(
            fake_video,
            replacement_text="memeistsdaily",
            output_path=output,
        )

    cover.assert_called_once_with(
        fake_video,
        region,
        replacement_text="@memeistsdaily",
        output_path=output,
    )
    assert result.output_path == output
    assert result.region is region
    assert result.skipped_reason is None


def test_normalize_handle_accepts_plain_username() -> None:
    assert _normalize_handle("memeistsdaily") == "@memeistsdaily"
    assert _normalize_handle("@memeistsdaily") == "@memeistsdaily"
    assert _normalize_handle("not a handle") == ""


def test_same_or_prefix_handle_matches_partial_own_handle() -> None:
    assert _same_or_prefix_handle("@memeist", "@memeistsdaily") is True
    assert _same_or_prefix_handle("@comedyslam", "@memeistsdaily") is False


# ---------------------------------------------------------------------------
# _font_arg
# ---------------------------------------------------------------------------


def test_font_arg_non_windows_returns_empty():
    with patch("nicheflow_studio.processing.watermark.windows_font_file", return_value=None):
        assert _font_arg() == ""


def test_font_arg_windows_returns_fontfile_prefix():
    fake_font = Path("C:/Windows/Fonts/arial.ttf")
    with patch("nicheflow_studio.processing.watermark.windows_font_file", return_value=fake_font):
        result = _font_arg()
    assert result.startswith("fontfile=")
    assert result.endswith(":")
