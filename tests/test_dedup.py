"""Tests for content-level video dedup (processing/dedup.py).

Frame loading is monkeypatched so the fingerprint + match logic is exercised
without real video.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from nicheflow_studio.processing import dedup
from nicheflow_studio.processing.video import VideoProbe


def _frame(seed: int, *, size: int = 120) -> np.ndarray:
    """Deterministic noisy frame; different seeds => different footage."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(size, size, 3), dtype=np.uint8)


def test_fingerprints_match_self_and_reject_unrelated() -> None:
    fp_a = "a1b2c3d4e5f60718,1122334455667788,99aabbccddeeff00"
    fp_b = "0f0f0f0f0f0f0f0f,f0f0f0f0f0f0f0f0,1234123412341234"
    assert dedup.fingerprints_match(fp_a, fp_a) is True
    assert dedup.fingerprints_match(fp_a, fp_b) is False


def test_fingerprints_match_tolerates_small_per_frame_differences() -> None:
    fp = "a1b2c3d4e5f60718,1122334455667788"
    near = "a1b2c3d4e5f60719,1122334455667789"  # 1 bit off in each frame
    assert dedup.fingerprints_match(fp, near) is True


def test_empty_or_missing_fingerprints_never_match() -> None:
    assert dedup.fingerprints_match(None, "a1b2c3d4e5f60718") is False
    assert dedup.fingerprints_match("", "") is False


def test_compute_fingerprint_is_stable_and_distinguishes_clips(monkeypatch) -> None:
    probe = VideoProbe(width=120, height=120, duration_seconds=10.0)
    monkeypatch.setattr(dedup, "probe_video", lambda _p: probe)

    # Clip 1: the same frame at every sampled position.
    monkeypatch.setattr(dedup, "_load_video_frame_at", lambda _p, _t: _frame(1))
    fp1a = dedup.compute_video_fingerprint(Path("clip1.mp4"))
    fp1b = dedup.compute_video_fingerprint(Path("clip1_repost.mp4"))  # same footage
    assert fp1a and fp1a == fp1b
    assert dedup.fingerprints_match(fp1a, fp1b) is True

    # Clip 2: different footage at every position.
    monkeypatch.setattr(dedup, "_load_video_frame_at", lambda _p, t: _frame(int(t) + 100))
    fp2 = dedup.compute_video_fingerprint(Path("clip2.mp4"))
    assert fp2 is not None
    assert dedup.fingerprints_match(fp1a, fp2) is False


def test_compute_fingerprint_none_when_unreadable(monkeypatch) -> None:
    probe = VideoProbe(width=120, height=120, duration_seconds=10.0)
    monkeypatch.setattr(dedup, "probe_video", lambda _p: probe)
    monkeypatch.setattr(dedup, "_load_video_frame_at", lambda _p, _t: None)
    assert dedup.compute_video_fingerprint(Path("dead.mp4")) is None


def test_find_matching_fingerprint_returns_first_hit() -> None:
    fp = "a1b2c3d4e5f60718,1122334455667788"
    candidates = [
        (1, "0000000000000000,1111111111111111"),
        (2, "a1b2c3d4e5f60719,1122334455667789"),  # matches fp
        (3, "ffffffffffffffff,eeeeeeeeeeeeeeee"),
    ]
    hit = dedup.find_matching_fingerprint(fp, candidates)
    assert hit is not None and hit[0] == 2
