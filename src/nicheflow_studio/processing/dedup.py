"""Content-level video dedup via a perceptual fingerprint.

URL/shortcode dedup (``MediaAsset.canonical_source_url`` / ``source_shortcode``)
only catches the *same post*. The same underlying clip gets reposted by multiple
source accounts under different shortcodes — URL dedup misses those. This module
computes a small perceptual fingerprint from a handful of frames so "is this the
same footage?" works across reposts, re-encodes, and different overlay text.

Approach: sample a few frames, **center-crop** each (drop the top hook-text band,
bottom subtitles, and black borders so we compare the actual footage), reduce to
a tiny grayscale, and compute a 64-bit difference hash (dHash). Two clips are the
same if enough of their frame hashes match within a small Hamming distance.

Pure NumPy + the existing PyAV frame loader — no OpenCV dependency, so dedup
works even where face detection doesn't.
"""
from __future__ import annotations

from pathlib import Path

from nicheflow_studio.processing.video import VideoProbe, _load_video_frame_at, probe_video

# Relative positions to sample. Spread across the clip so a trim at either end
# still leaves overlapping frames between two versions of the same footage.
SAMPLE_POSITIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
# Center crop fractions (drop hook text at top, subtitles at bottom, side bars).
CROP_X = (0.20, 0.80)
CROP_Y = (0.25, 0.75)
# dHash grid: comparing 9 columns across 8 rows yields 8*8 = 64 bits.
HASH_W, HASH_H = 9, 8
# Two frame hashes within this many bits (of 64) are "the same frame".
FRAME_MATCH_THRESHOLD = 10
# This many matching frames mark two clips as the same footage.
MIN_FRAME_MATCHES = 2


def _resize_gray_nearest(gray, target_w: int, target_h: int):
    """Nearest-neighbour downscale to (target_h, target_w). Good enough for dHash."""
    import numpy as np

    height, width = gray.shape
    ys = np.linspace(0, height - 1, target_h).astype(int)
    xs = np.linspace(0, width - 1, target_w).astype(int)
    return gray[ys][:, xs]


def _frame_dhash(frame) -> int:
    """64-bit difference hash of a center-cropped frame."""
    import numpy as np

    gray = frame.astype(np.float32).mean(axis=2)
    height, width = gray.shape
    y0, y1 = int(height * CROP_Y[0]), int(height * CROP_Y[1])
    x0, x1 = int(width * CROP_X[0]), int(width * CROP_X[1])
    cropped = gray[y0:y1, x0:x1]
    if cropped.size == 0:
        cropped = gray
    small = _resize_gray_nearest(cropped, HASH_W, HASH_H)
    diff = small[:, 1:] > small[:, :-1]
    bits = 0
    for value in diff.flatten():
        bits = (bits << 1) | int(value)
    return bits


def compute_video_fingerprint(
    path: Path, probe: VideoProbe | None = None
) -> str | None:
    """Perceptual fingerprint for a video, or ``None`` if it can't be read.

    Returned as comma-separated 16-hex-char frame hashes, e.g. ``"a1..,c3.."``.
    Safe to store in a small text column.
    """
    path = Path(path)
    try:
        probe = probe or probe_video(path)
    except Exception:  # noqa: BLE001 - unreadable video => no fingerprint
        return None
    duration = max(probe.duration_seconds, 0.0)
    if duration <= 0:
        return None

    hashes: list[int] = []
    for position in SAMPLE_POSITIONS:
        frame = _load_video_frame_at(path, duration * position)
        if frame is None:
            continue
        try:
            hashes.append(_frame_dhash(frame))
        except Exception:  # noqa: BLE001
            continue
    if not hashes:
        return None
    return ",".join(f"{value:016x}" for value in hashes)


def safe_video_fingerprint(path: Path) -> str | None:
    """Best-effort :func:`compute_video_fingerprint` that never raises.

    Download paths call this so a fingerprinting failure (unreadable file,
    missing codec) can never break the download or asset registration — the
    asset just keeps a ``None`` ``content_hash`` and is excluded from footage
    dedup until it is re-fingerprinted.
    """
    try:
        return compute_video_fingerprint(Path(path))
    except Exception:  # noqa: BLE001 - fingerprinting is always best-effort
        return None


def _parse_fingerprint(fingerprint: str | None) -> list[int]:
    if not fingerprint:
        return []
    out: list[int] = []
    for part in fingerprint.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part, 16))
        except ValueError:
            continue
    return out


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def fingerprints_match(
    a: str | None,
    b: str | None,
    *,
    frame_threshold: int = FRAME_MATCH_THRESHOLD,
    min_matches: int = MIN_FRAME_MATCHES,
) -> bool:
    """True if two fingerprints share enough near-identical frames."""
    ha = _parse_fingerprint(a)
    hb = _parse_fingerprint(b)
    if not ha or not hb:
        return False
    matches = 0
    for x in ha:
        if any(_hamming(x, y) <= frame_threshold for y in hb):
            matches += 1
            if matches >= min_matches:
                return True
    return False


def find_matching_fingerprint(
    fingerprint: str | None,
    candidates,
    *,
    frame_threshold: int = FRAME_MATCH_THRESHOLD,
    min_matches: int = MIN_FRAME_MATCHES,
):
    """Return the first ``(key, fp)`` candidate that matches, or ``None``.

    ``candidates`` is an iterable of ``(key, fingerprint)`` pairs — the key is
    whatever the caller wants back (e.g. a MediaAsset id).
    """
    if not fingerprint:
        return None
    for key, candidate_fp in candidates:
        if fingerprints_match(
            fingerprint, candidate_fp, frame_threshold=frame_threshold, min_matches=min_matches
        ):
            return key, candidate_fp
    return None
