"""Layout-variant matrix for ``detect_content_rectangle``.

The detector is a heuristic stack tuned against real reels, and every past
framing bug added another constant to it. This module pins the behaviour on a
spread of synthetic canvas layouts so a future tuning change has to declare its
blast radius: variants that work stay working, and the ones that are known to be
wrong are marked ``xfail(strict=True)`` so fixing one fails the suite instead of
passing silently.

Frames are built from seeded noise rather than fixtures on disk, so the matrix
runs without ffmpeg and without the download library.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nicheflow_studio.processing import video

np = pytest.importorskip("numpy")

FRAME_HEIGHT = 640
FRAME_WIDTH = 360
# The detector samples 7 frames; layouts vary "animated" regions across them and
# hold "static" regions fixed, which is the signal the whole stack keys on.
FRAME_COUNT = 7
# Detected edges land within a row or two of the drawn edge, plus the descender
# padding the detector deliberately adds at the bottom.
EDGE_TOLERANCE = 25


def _noise(seed: int, height: int, width: int, *, low: int = 60, high: int = 255):
    """Deterministic per-seed fill. Same seed across frames == a static region."""
    return np.random.default_rng(seed).integers(
        low, high, size=(height, width, 3), dtype=np.uint8
    )


def _blur_axis(values, radius: int, axis: int):
    """Box blur along one axis via a cumulative-sum sliding window."""
    length = values.shape[axis]
    padding = [(0, 0)] * values.ndim
    padding[axis] = (radius, radius)
    sums = np.cumsum(np.pad(values, padding, mode="edge"), axis=axis, dtype=np.float64)
    zeros_shape = list(sums.shape)
    zeros_shape[axis] = 1
    sums = np.concatenate([np.zeros(zeros_shape), sums], axis=axis)
    window = 2 * radius + 1
    low = np.take(sums, np.arange(0, length), axis=axis)
    high = np.take(sums, np.arange(window, window + length), axis=axis)
    return (high - low) / window


def _blur(values, radius: int = 12, passes: int = 3):
    blurred = values.astype(np.float64)
    for _ in range(passes):
        blurred = _blur_axis(_blur_axis(blurred, radius, 0), radius, 1)
    return blurred.astype(np.uint8)


def _canvas(level: int = 0):
    return np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), level, dtype=np.uint8)


# --- Layouts: each takes a frame index and returns one sampled frame -------- #


def _full_frame_footage(index: int):
    """A raw clip that already fills the frame: nothing to crop away."""
    return _noise(index, FRAME_HEIGHT, FRAME_WIDTH)


def _plain_letterbox(index: int):
    """Footage centred on a black canvas, no text anywhere."""
    frame = _canvas()
    frame[200:440, :, :] = _noise(index, 240, FRAME_WIDTH)
    return frame


def _repost_card_gapped(index: int):
    """Screenshot-repost card whose rows animate, each split by a canvas gap.

    The layout that shipped a broken crop to a live post: gap bridging welds the
    handle row and every caption line onto the footage band.
    """
    frame = _canvas()
    frame[60:80, 30:150, :] = _noise(100 + index, 20, 120)
    for line_top in (100, 125, 150):
        frame[line_top : line_top + 15, 30:330, :] = _noise(200 + index + line_top, 15, 300)
    frame[185:500, 20:340, :] = _noise(index, 315, 320)
    return frame


def _repost_card_contiguous(index: int):
    """The same card with tight leading, so the caption is one unbroken run."""
    frame = _canvas()
    frame[100:166, 30:330, :] = _noise(200 + index, 66, 300)
    frame[185:500, 20:340, :] = _noise(index, 315, 320)
    return frame


def _static_dark_title_on_light_canvas(index: int):
    """Light canvas with a motionless dark title block above the footage."""
    frame = _canvas(245)
    frame[40:120, 40:320, :] = _noise(7, 80, 280, low=0, high=40)
    frame[200:480, 20:340, :] = _noise(index, 280, 320)
    return frame


def _attached_dark_banner(index: int):
    """A baked-in banner touching the footage: part of the clip, must be kept."""
    frame = _canvas()
    frame[170:200, 20:340, :] = _noise(7, 30, 320, low=0, high=60)
    frame[170:200, 60:300, :] = 235
    frame[200:480, 20:340, :] = _noise(index, 280, 320)
    return frame


def _bottom_caption_gapped(index: int):
    """An animated caption line under the footage, separated by a canvas gap."""
    frame = _canvas()
    frame[150:430, 20:340, :] = _noise(index, 280, 320)
    frame[450:470, 30:330, :] = _noise(300 + index, 20, 300)
    return frame


def _two_panel_grid(index: int):
    """Stacked before/after panels: both belong to the footage."""
    frame = _canvas()
    frame[100:340, 20:340, :] = _noise(index, 240, 320)
    frame[350:590, 20:340, :] = _noise(500 + index, 240, 320)
    return frame


def _text_longer_than_footage(index: int):
    """A caption block taller than the clip it introduces."""
    frame = _canvas()
    frame[60:400, 30:330, :] = _noise(200 + index, 340, 300)
    frame[430:560, 20:340, :] = _noise(index, 130, 320)
    return frame


def _static_footage_animated_card(index: int):
    """A still-photo reel under an animated card: the only motion is the card."""
    frame = _canvas()
    for line_top in (60, 90, 120):
        frame[line_top : line_top + 20, 30:330, :] = _noise(200 + index + line_top, 20, 300)
    frame[200:500, 20:340, :] = _noise(7, 300, 320)
    return frame


def _blurred_background(index: int):
    """A landscape clip over a heavy-blurred enlargement of itself."""
    clip = _noise(index, 240, 320)
    fill = np.repeat(np.repeat(clip, 3, axis=0), 3, axis=1)[:FRAME_HEIGHT, :FRAME_WIDTH, :]
    frame = _blur(fill)
    frame[200:440, 20:340, :] = clip
    return frame


def _detect(monkeypatch, build) -> video.CropSettings | None:
    frames: dict[int, object] = {}

    def fake_load(path, timestamp):  # noqa: ANN001, ARG001
        index = int(round(float(timestamp) * 3)) % FRAME_COUNT
        if index not in frames:
            frames[index] = build(index)
        return frames[index]

    monkeypatch.setattr(video, "_load_video_frame_at", fake_load)
    probe = video.VideoProbe(width=FRAME_WIDTH, height=FRAME_HEIGHT, duration_seconds=20.0)
    return video.detect_content_rectangle(Path("layout.mp4"), probe)


_KNOWN_CONTIGUOUS = pytest.mark.xfail(
    strict=True,
    reason="A caption block with no internal gaps is one run longer than "
    "CONTENT_RECT_TOP_LINE_MAX_RATIO, so the leading-run walk stops on it.",
)
_KNOWN_TALL_TEXT = pytest.mark.xfail(
    strict=True,
    reason="_largest_coverage_band picks the caption block as the band before "
    "any top trim runs, because it is longer than the footage.",
)
_KNOWN_STATIC_FOOTAGE = pytest.mark.xfail(
    strict=True,
    reason="Motion is the primary signal; a still-photo reel under an animated "
    "card inverts the ranking and the card wins.",
)


@pytest.mark.parametrize(
    ("build", "expected_top"),
    [
        pytest.param(_plain_letterbox, 200, id="plain_letterbox"),
        pytest.param(_repost_card_gapped, 185, id="repost_card_gapped"),
        pytest.param(
            _repost_card_contiguous, 185, id="repost_card_contiguous", marks=_KNOWN_CONTIGUOUS
        ),
        pytest.param(_static_dark_title_on_light_canvas, 200, id="static_title_light_canvas"),
        pytest.param(_attached_dark_banner, 170, id="attached_dark_banner_kept"),
        pytest.param(_bottom_caption_gapped, 150, id="bottom_caption_gapped"),
        pytest.param(_two_panel_grid, 100, id="two_panel_grid"),
        pytest.param(
            _text_longer_than_footage, 430, id="text_longer_than_footage", marks=_KNOWN_TALL_TEXT
        ),
        pytest.param(
            _static_footage_animated_card,
            200,
            id="static_footage_animated_card",
            marks=_KNOWN_STATIC_FOOTAGE,
        ),
        pytest.param(_blurred_background, 200, id="blurred_background"),
    ],
)
def test_content_rectangle_top_edge_by_layout(monkeypatch, build, expected_top: int) -> None:
    crop = _detect(monkeypatch, build)

    assert crop is not None
    assert abs(crop.top - expected_top) <= EDGE_TOLERANCE


def test_content_rectangle_returns_empty_for_full_frame_footage(monkeypatch) -> None:
    """Footage filling the frame yields no crop, so callers skip the rectangle."""
    crop = _detect(monkeypatch, _full_frame_footage)

    assert crop == video.CropSettings()


def test_repost_card_regresses_without_the_leading_run_trim(monkeypatch) -> None:
    """The card layout must stay broken with the trim disabled.

    Guards the guard: a ratio of 0 caps the allowed run length at one row, so the
    walk breaks on the first run and the band keeps its bridged top edge — the
    behaviour that shipped a card-framed reel to a live post. If this ever stops
    failing, the matrix above has gone blind to the bug it exists to catch.
    """
    monkeypatch.setattr(video, "CONTENT_RECT_TOP_LINE_MAX_RATIO", 0.0)

    crop = _detect(monkeypatch, _repost_card_gapped)

    assert crop is not None
    # Lands on the avatar row at 60 rather than the footage at 185.
    assert crop.top < 100


@pytest.mark.xfail(
    strict=True,
    reason="The leading-thin-run trim is applied to the top edge only, so a "
    "gapped caption under the footage is still bridged into the band.",
)
def test_content_rectangle_drops_bottom_caption_line(monkeypatch) -> None:
    crop = _detect(monkeypatch, _bottom_caption_gapped)

    assert crop is not None
    # Footage ends at row 430 of 640, so anything below is canvas plus caption.
    assert crop.bottom >= FRAME_HEIGHT - 430 - EDGE_TOLERANCE
