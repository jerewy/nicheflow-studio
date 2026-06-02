"""Auto-pick a clean (text-free) cover image for a processed reel.

The processed output has the hook title burned onto every frame, so a clean
cover has to come from the *original* source clip. We score sampled source
frames for sharpness and exposure, pick the best one, then crop it to the reel's
framing (the same crop the reel was exported with) and save it next to the
output video. The chosen timestamp is returned too, so a future publish path can
reuse it.

Reuses the existing PyAV frame loader and ffmpeg crop math from ``video.py`` —
no new dependencies, and the cover is pixel-aligned with the reel.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.media_tools import ffmpeg_binary, subprocess_run_kwargs
from nicheflow_studio.processing.video import (
    CropSettings,
    VideoProbe,
    _load_video_frame_at,
    detect_content_rectangle,
    output_dimensions,
    probe_video,
)

# How many evenly-spaced source frames to score. More = better pick, slower.
COVER_SAMPLE_COUNT = 24
# Skip the very start/end (intros, outros, fades, black frames) when sampling.
COVER_EDGE_SKIP_FRACTION = 0.05

# --- Exposure gate ---------------------------------------------------------
# A good cover sits in a comfortable mid-brightness band. Below GOOD_LOW it
# ramps down hard (dark cinematic frames make weak covers); above GOOD_HIGH it
# ramps down for blown-out frames. The floor keeps a dark frame from scoring 0.
EXPOSURE_GOOD_LOW = 70.0
EXPOSURE_GOOD_HIGH = 200.0
EXPOSURE_DARK_FLOOR = 20.0
EXPOSURE_BRIGHT_CEIL = 245.0
EXPOSURE_MIN = 0.05

# --- Face bonus ------------------------------------------------------------
# A prominent, centred face multiplies a frame's base score. GAIN converts
# "face area x centrality" into a multiplier; MAX caps it so a face can't fully
# override sharpness/exposure. Tuned so a clear centred face roughly doubles
# the score (faces win ties for movie/history covers).
FACE_BONUS_GAIN = 8.0
FACE_BONUS_MAX = 2.0
FACE_DETECT_WIDTH = 480
# Tightened to cut false positives on dark/noisy frames: more neighbour
# agreement, and faces must occupy a real fraction of the (cropped) frame.
FACE_MIN_NEIGHBORS = 7
FACE_MIN_SIZE_FRACTION = 0.08

# --- Text penalty (avoid burned-in subtitles) ------------------------------
# Subtitles are bright, high-contrast strokes clustered in the lower-centre of
# the frame. We measure bright-edge density there and penalise frames that look
# like they carry text, so the picker prefers a clean, text-free moment.
TEXT_BAND_TOP_FRACTION = 0.55  # only inspect the lower ~45% of the frame
TEXT_BAND_SIDE_MARGIN = 0.10  # ignore the outer 10% on each side
TEXT_BRIGHTNESS_MIN = 205.0
TEXT_EDGE_MIN = 40.0
TEXT_DENSITY_LOW = 0.004  # at/below this => treat as clean (penalty 1.0)
TEXT_DENSITY_HIGH = 0.020  # at/above this => full penalty
TEXT_PENALTY_MIN = 0.25  # how hard a clearly-texted frame is down-weighted

# --- Cover crop ------------------------------------------------------------
# Shave this fraction off each side of the detected content rectangle to drop
# rounded corners and any residual repost border.
COVER_CROP_INSET_FRACTION = 0.015

# Lazy, cached Haar cascade. None means OpenCV (or the cascade) is unavailable,
# in which case cover picking silently falls back to sharpness/exposure only.
_FACE_CASCADE = None
_FACE_CASCADE_LOADED = False


def _face_cascade():
    """Return a cached frontal-face Haar cascade, or None if unavailable."""
    global _FACE_CASCADE, _FACE_CASCADE_LOADED
    if _FACE_CASCADE_LOADED:
        return _FACE_CASCADE
    _FACE_CASCADE_LOADED = True
    try:
        import cv2

        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        _FACE_CASCADE = None if cascade.empty() else cascade
    except Exception:  # noqa: BLE001 - OpenCV missing/broken => no face boost
        _FACE_CASCADE = None
    return _FACE_CASCADE


def face_detection_available() -> bool:
    """True when OpenCV + the Haar cascade loaded, so face-aware picking is active.

    Lets startup log whether the face boost is on, instead of it silently
    degrading to sharpness/exposure in a packaged build that didn't bundle cv2.
    """
    return _face_cascade() is not None


def _detect_faces(frame) -> list[tuple[int, int, int, int]]:
    """Detect faces in an RGB frame; returns (x, y, w, h) boxes in frame coords.

    Empty list when OpenCV is unavailable or no face is found — callers treat
    that as "no face bonus", never as an error.
    """
    cascade = _face_cascade()
    if cascade is None:
        return []
    try:
        import cv2

        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape[:2]
        scale = 1.0
        if width > FACE_DETECT_WIDTH:
            scale = FACE_DETECT_WIDTH / float(width)
            gray = cv2.resize(gray, (int(width * scale), int(height * scale)))
        # Require a real-sized face and more neighbour agreement so dark/noisy
        # frames don't earn a bonus from a spurious "face".
        detect_h, detect_w = gray.shape[:2]
        min_side = max(int(min(detect_w, detect_h) * FACE_MIN_SIZE_FRACTION), 30)
        detections = cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=FACE_MIN_NEIGHBORS,
            minSize=(min_side, min_side),
        )
        return [
            (int(x / scale), int(y / scale), int(w / scale), int(h / scale))
            for (x, y, w, h) in detections
        ]
    except Exception:  # noqa: BLE001
        return []


def _face_bonus(frame) -> float:
    """Score multiplier (>= 1.0) rewarding a large, centred face in the frame."""
    faces = _detect_faces(frame)
    if not faces:
        return 1.0
    height, width = frame.shape[:2]
    frame_area = float(width * height) or 1.0
    best = 0.0
    for x, y, w, h in faces:
        area_fraction = (w * h) / frame_area
        center_x = (x + w / 2.0) / width
        center_y = (y + h / 2.0) / height
        # 1.0 dead-centre, lower toward the edges (floored so off-centre faces
        # still earn most of their area bonus).
        centrality = max(1.0 - (abs(center_x - 0.5) + abs(center_y - 0.5)), 0.4)
        best = max(best, area_fraction * centrality)
    return 1.0 + min(best * FACE_BONUS_GAIN, FACE_BONUS_MAX)


@dataclass(frozen=True)
class CoverCandidate:
    """One ranked source frame extracted as a possible cover."""

    image_path: Path
    timestamp_seconds: float
    score: float


@dataclass(frozen=True)
class CoverResult:
    """A generated cover: the saved image and the source timestamp it came from."""

    image_path: Path
    timestamp_seconds: float
    candidates: tuple[CoverCandidate, ...] = ()


def _exposure_weight(mean_brightness: float) -> float:
    """0.05-1.0 weight that ramps down hard for dark and blown-out frames."""
    if mean_brightness < EXPOSURE_GOOD_LOW:
        ramp = (mean_brightness - EXPOSURE_DARK_FLOOR) / (EXPOSURE_GOOD_LOW - EXPOSURE_DARK_FLOOR)
        return max(ramp, EXPOSURE_MIN)
    if mean_brightness > EXPOSURE_GOOD_HIGH:
        ramp = (EXPOSURE_BRIGHT_CEIL - mean_brightness) / (
            EXPOSURE_BRIGHT_CEIL - EXPOSURE_GOOD_HIGH
        )
        return max(ramp, EXPOSURE_MIN)
    return 1.0


def _score_frame(frame) -> float:
    """Higher = a better cover. Sharpness (Laplacian variance) x exposure gate."""
    import numpy as np

    gray = frame.astype(np.float32).mean(axis=2)
    laplacian = (
        -4.0 * gray
        + np.roll(gray, 1, axis=0)
        + np.roll(gray, -1, axis=0)
        + np.roll(gray, 1, axis=1)
        + np.roll(gray, -1, axis=1)
    )
    sharpness = float(laplacian.var())
    return sharpness * _exposure_weight(float(gray.mean()))


def _crop_frame(frame, crop: CropSettings):
    """Crop an (H, W, 3) frame by the inset CropSettings; safe against bad insets."""
    height, width = frame.shape[:2]
    top = min(max(crop.top, 0), height - 1)
    bottom = min(max(crop.bottom, 0), height - 1)
    left = min(max(crop.left, 0), width - 1)
    right = min(max(crop.right, 0), width - 1)
    cropped = frame[top : height - bottom, left : width - right]
    return cropped if cropped.size else frame


def _text_penalty(frame) -> float:
    """<= 1.0 multiplier that down-weights frames carrying burned-in subtitles.

    Subtitles are bright, high-contrast vertical strokes clustered in the
    lower-centre of the frame. We measure the density of bright *edges* there;
    a clean frame has almost none, a captioned frame has a lot.
    """
    import numpy as np

    gray = frame.astype(np.float32).mean(axis=2)
    height, width = gray.shape[:2]
    row0 = int(height * TEXT_BAND_TOP_FRACTION)
    col0 = int(width * TEXT_BAND_SIDE_MARGIN)
    col1 = int(width * (1.0 - TEXT_BAND_SIDE_MARGIN))
    band = gray[row0:height, col0:col1]
    if band.size == 0 or band.shape[1] < 2:
        return 1.0
    bright = band[:, 1:] > TEXT_BRIGHTNESS_MIN
    edges = np.abs(np.diff(band, axis=1)) > TEXT_EDGE_MIN
    density = float((bright & edges).mean())
    if density <= TEXT_DENSITY_LOW:
        return 1.0
    if density >= TEXT_DENSITY_HIGH:
        return TEXT_PENALTY_MIN
    fraction = (density - TEXT_DENSITY_LOW) / (TEXT_DENSITY_HIGH - TEXT_DENSITY_LOW)
    return 1.0 - fraction * (1.0 - TEXT_PENALTY_MIN)


def _inset_crop(crop: CropSettings, probe: VideoProbe) -> CropSettings:
    """Shave a small margin off each side to drop rounded corners / border."""
    dx = int(probe.width * COVER_CROP_INSET_FRACTION)
    dy = int(probe.height * COVER_CROP_INSET_FRACTION)
    inset = CropSettings(
        left=crop.left + dx, top=crop.top + dy, right=crop.right + dx, bottom=crop.bottom + dy
    )
    try:
        output_dimensions(probe, inset)
        return inset
    except Exception:  # noqa: BLE001 - inset too aggressive, keep original
        return crop


def resolve_cover_crop(
    source_path: Path,
    probe: VideoProbe | None = None,
    *,
    fallback: CropSettings | None = None,
) -> CropSettings:
    """Crop that isolates the inner footage: content rectangle, slightly inset.

    Removes the repost's black canvas and top hook-text band so the cover is the
    full picture with no border. Falls back to ``fallback`` (the reel crop) or no
    crop when the content rectangle can't be detected.
    """
    source_path = Path(source_path)
    probe = probe or probe_video(source_path)
    crop: CropSettings | None = None
    try:
        crop = detect_content_rectangle(source_path, probe)
    except Exception:  # noqa: BLE001
        crop = None
    if crop is None:
        crop = fallback or CropSettings()
    try:
        output_dimensions(probe, crop)
    except Exception:  # noqa: BLE001 - invalid crop, give up on cropping
        crop = CropSettings()
    return _inset_crop(crop, probe)


def pick_cover_timestamp(
    source_path: Path,
    probe: VideoProbe | None = None,
    *,
    crop: CropSettings | None = None,
    penalize_text: bool = True,
    sample_count: int = COVER_SAMPLE_COUNT,
) -> float:
    """Return the timestamp (seconds) of the best-scoring frame.

    When ``crop`` is given, every frame is cropped to the content region before
    scoring. ``penalize_text=False`` disables the subtitle penalty — used for
    hook-on-cover, where the burned-in hook text is *wanted* on the cover.
    """
    source_path = Path(source_path)
    probe = probe or probe_video(source_path)
    duration = max(probe.duration_seconds, 0.0)
    if duration <= 0:
        return 0.0

    start = duration * COVER_EDGE_SKIP_FRACTION
    end = duration * (1.0 - COVER_EDGE_SKIP_FRACTION)
    span = max(end - start, 0.0)
    if span <= 0 or sample_count <= 1:
        return duration / 2.0

    best_timestamp = start
    best_score = -1.0
    for index in range(sample_count):
        timestamp = start + span * (index / (sample_count - 1))
        frame = _load_video_frame_at(source_path, timestamp)
        if frame is None:
            continue
        if crop is not None:
            frame = _crop_frame(frame, crop)
        # Sharp + well-exposed, boosted by a prominent face. The subtitle penalty
        # only applies for clean covers; hook covers keep the text on purpose.
        score = _score_frame(frame) * _face_bonus(frame)
        if penalize_text:
            score *= _text_penalty(frame)
        if score > best_score:
            best_score = score
            best_timestamp = timestamp
    return best_timestamp


def cover_path_for_output(output_video_path: Path) -> Path:
    """The conventional cover-image path under the account's cover folder."""
    output_video_path = Path(output_video_path)
    return output_video_path.parent / "covers" / f"{output_video_path.stem}_cover.jpg"


def cover_candidate_path_for_output(output_video_path: Path, index: int) -> Path:
    """A ranked candidate image path under the account's cover folder."""
    output_video_path = Path(output_video_path)
    return (
        output_video_path.parent
        / "covers"
        / f"{output_video_path.stem}_cover_candidate_{index:02d}.jpg"
    )


def legacy_cover_path_for_output(output_video_path: Path) -> Path:
    """Old pre-cover-folder path kept as a read fallback for existing exports."""
    output_video_path = Path(output_video_path)
    return output_video_path.with_name(f"{output_video_path.stem}_cover.jpg")


def legacy_cover_candidate_path_for_output(output_video_path: Path, index: int) -> Path:
    """Old pre-cover-folder candidate path kept as a read fallback."""
    output_video_path = Path(output_video_path)
    return output_video_path.with_name(f"{output_video_path.stem}_cover_candidate_{index:02d}.jpg")


def _rank_cover_timestamps(
    source_path: Path,
    probe: VideoProbe,
    *,
    crop: CropSettings | None,
    sample_count: int,
    penalize_text: bool = True,
) -> list[tuple[float, float]]:
    """Return sampled timestamps ranked by cover score, highest first.

    ``penalize_text=False`` keeps frames with burned-in text (hook covers want
    the hook visible); the default penalises text (clean covers).
    """
    duration = max(probe.duration_seconds, 0.0)
    if duration <= 0:
        return [(0.0, 0.0)]

    start = duration * COVER_EDGE_SKIP_FRACTION
    end = duration * (1.0 - COVER_EDGE_SKIP_FRACTION)
    span = max(end - start, 0.0)
    if span <= 0 or sample_count <= 1:
        return [(duration / 2.0, 0.0)]

    ranked: list[tuple[float, float]] = []
    for index in range(sample_count):
        timestamp = start + span * (index / (sample_count - 1))
        frame = _load_video_frame_at(source_path, timestamp)
        if frame is None:
            continue
        if crop is not None:
            frame = _crop_frame(frame, crop)
        score = _score_frame(frame) * _face_bonus(frame)
        if penalize_text:
            score *= _text_penalty(frame)
        ranked.append((timestamp, score))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def extract_cover_at(
    *,
    source_path: Path,
    cover_path: Path,
    crop: CropSettings,
    timestamp_seconds: float,
    probe: VideoProbe | None = None,
) -> bool:
    """Extract one source frame at ``timestamp_seconds``, cropped to reel framing.

    Used both for the auto-pick and the manual override (user-chosen timestamp).
    Returns True if the cover image was written.
    """
    source_path = Path(source_path)
    cover_path = Path(cover_path)
    ffmpeg = ffmpeg_binary()
    if ffmpeg is None:
        return False
    probe = probe or probe_video(source_path)
    crop_width, crop_height = output_dimensions(probe, crop)
    crop_filter = f"crop={crop_width}:{crop_height}:{crop.left}:{crop.top}"
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-y",
        "-ss",
        f"{max(timestamp_seconds, 0.0):.3f}",
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-vf",
        crop_filter,
        "-q:v",
        "2",
        str(cover_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, **subprocess_run_kwargs())
    except Exception:  # noqa: BLE001 - any ffmpeg failure just means "no cover"
        return False
    return cover_path.exists()


def generate_cover_candidates(
    *,
    source_path: Path,
    output_video_path: Path,
    crop: CropSettings,
    probe: VideoProbe | None = None,
    candidate_count: int = 6,
    sample_count: int = COVER_SAMPLE_COUNT,
) -> tuple[CoverCandidate, ...]:
    """Extract the best ranked candidate covers beside ``output_video_path``."""
    if candidate_count <= 0:
        return ()
    source_path = Path(source_path)
    probe = probe or probe_video(source_path)
    cover_crop = resolve_cover_crop(source_path, probe, fallback=crop)
    ranked = _rank_cover_timestamps(
        source_path,
        probe,
        crop=cover_crop,
        sample_count=sample_count,
    )
    candidates: list[CoverCandidate] = []
    for index, (timestamp, score) in enumerate(ranked[:candidate_count], start=1):
        candidate_path = cover_candidate_path_for_output(output_video_path, index)
        if extract_cover_at(
            source_path=source_path,
            cover_path=candidate_path,
            crop=cover_crop,
            timestamp_seconds=timestamp,
            probe=probe,
        ):
            candidates.append(
                CoverCandidate(
                    image_path=candidate_path,
                    timestamp_seconds=timestamp,
                    score=score,
                )
            )
    return tuple(candidates)


def generate_cover(
    *,
    source_path: Path,
    output_video_path: Path,
    crop: CropSettings,
    probe: VideoProbe | None = None,
    candidate_count: int = 6,
    strategy: str = "hook",
) -> CoverResult | None:
    """Auto-pick and save a cover beside ``output_video_path``.

    ``strategy``:
    - ``"hook"`` (default): the cover is a frame of the *finished reel*, so the
      burned-in hook text shows in the grid — matching how the reference accounts
      (and our own pastmomentsdaily) post. Scored on sharpness/exposure/face; the
      text penalty is off because the hook is wanted.
    - ``"clean"``: a text-free frame from the original clip, content-cropped (the
      previous behaviour), for when a clean cover is preferred.

    Returns the ``CoverResult`` (image path + chosen timestamp), or ``None`` if a
    cover could not be produced (cover generation never blocks processing).
    """
    source_path = Path(source_path)
    output_video_path = Path(output_video_path)

    if strategy == "clean":
        frame_video = source_path
        frame_probe = probe or probe_video(source_path)
        # Tight content crop (black canvas + top text removed); reel crop fallback.
        cover_crop = resolve_cover_crop(source_path, frame_probe, fallback=crop)
        penalize_text = True
    else:  # "hook" — sample the finished reel; the hook is on every frame.
        frame_video = output_video_path
        try:
            frame_probe = probe_video(output_video_path)
        except Exception:  # noqa: BLE001 - can't probe output, no cover
            return None
        cover_crop = CropSettings()  # the reel is already framed
        penalize_text = False

    ranked = _rank_cover_timestamps(
        frame_video,
        frame_probe,
        crop=cover_crop,
        sample_count=COVER_SAMPLE_COUNT,
        penalize_text=penalize_text,
    )
    timestamp = ranked[0][0] if ranked else 0.0
    cover_path = cover_path_for_output(output_video_path)
    if extract_cover_at(
        source_path=frame_video,
        cover_path=cover_path,
        crop=cover_crop,
        timestamp_seconds=timestamp,
        probe=frame_probe,
    ):
        candidates: list[CoverCandidate] = []
        for index, (candidate_timestamp, score) in enumerate(
            ranked[: max(candidate_count, 0)], start=1
        ):
            candidate_path = cover_candidate_path_for_output(output_video_path, index)
            if extract_cover_at(
                source_path=frame_video,
                cover_path=candidate_path,
                crop=cover_crop,
                timestamp_seconds=candidate_timestamp,
                probe=frame_probe,
            ):
                candidates.append(
                    CoverCandidate(
                        image_path=candidate_path,
                        timestamp_seconds=candidate_timestamp,
                        score=score,
                    )
                )
        return CoverResult(
            image_path=cover_path,
            timestamp_seconds=timestamp,
            candidates=tuple(candidates),
        )
    return None
