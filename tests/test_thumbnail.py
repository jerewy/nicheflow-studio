"""Tests for clean-cover auto-pick (processing/thumbnail.py).

These avoid real ffmpeg/video: frame loading and the ffmpeg extract are
monkeypatched so the scoring and orchestration logic is what's exercised.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from nicheflow_studio.processing import thumbnail
from nicheflow_studio.processing.video import CropSettings, VideoProbe


def _checkerboard(amplitude: float, *, brightness: float = 128.0, size: int = 32) -> np.ndarray:
    """A mid-bright checkerboard; higher amplitude => sharper (higher Laplacian)."""
    rows, cols = np.indices((size, size))
    checker = np.where((rows + cols) % 2 == 0, 1.0, -1.0)
    gray = brightness + amplitude * checker
    gray = np.clip(gray, 0, 255)
    return np.stack([gray, gray, gray], axis=-1).astype(np.uint8)


def test_score_frame_prefers_sharp_well_exposed_frame() -> None:
    sharp = thumbnail._score_frame(_checkerboard(100.0))
    flat = thumbnail._score_frame(_checkerboard(0.0))  # uniform => blurry
    black = thumbnail._score_frame(_checkerboard(100.0, brightness=5.0))  # too dark

    assert sharp > flat
    assert sharp > black  # exposure penalty sinks the near-black frame


def test_pick_cover_timestamp_picks_highest_scoring_frame(monkeypatch) -> None:
    probe = VideoProbe(width=200, height=200, duration_seconds=10.0)
    monkeypatch.setattr(thumbnail, "probe_video", lambda _p: probe)

    # Sharpness amplitude peaks at t=5s, so the picked timestamp should be the
    # sampled frame closest to the middle of the clip.
    def fake_loader(_path, timestamp):
        # Keep brightness < text threshold so the text penalty stays neutral;
        # only sharpness varies, peaking at t=5.
        amplitude = max(0.0, 1.0 - abs(timestamp - 5.0) / 5.0) * 60.0
        return _checkerboard(amplitude)

    monkeypatch.setattr(thumbnail, "_load_video_frame_at", fake_loader)

    chosen = thumbnail.pick_cover_timestamp(Path("clip.mp4"), sample_count=21)
    assert abs(chosen - 5.0) < 0.5


def test_pick_cover_timestamp_zero_duration_is_safe(monkeypatch) -> None:
    probe = VideoProbe(width=200, height=200, duration_seconds=0.0)
    monkeypatch.setattr(thumbnail, "probe_video", lambda _p: probe)
    monkeypatch.setattr(thumbnail, "_load_video_frame_at", lambda *_a: None)
    assert thumbnail.pick_cover_timestamp(Path("clip.mp4")) == 0.0


def test_cover_path_for_output_sits_beside_output() -> None:
    cover = thumbnail.cover_path_for_output(Path("/data/processed/reel_001.mp4"))
    assert cover == Path("/data/processed/covers/reel_001_cover.jpg")


def test_cover_candidate_path_for_output_is_numbered() -> None:
    cover = thumbnail.cover_candidate_path_for_output(Path("/data/processed/reel_001.mp4"), 3)
    assert cover == Path("/data/processed/covers/reel_001_cover_candidate_03.jpg")


def test_extract_cover_at_writes_file(monkeypatch, tmp_path: Path) -> None:
    probe = VideoProbe(width=200, height=400, duration_seconds=10.0)
    monkeypatch.setattr(thumbnail, "ffmpeg_binary", lambda: Path("ffmpeg"))

    cover_path = tmp_path / "reel_cover.jpg"
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"jpeg-bytes")  # last arg is the output path

    monkeypatch.setattr(thumbnail.subprocess, "run", fake_run)

    ok = thumbnail.extract_cover_at(
        source_path=tmp_path / "clip.mp4",
        cover_path=cover_path,
        crop=CropSettings(left=10, top=20, right=10, bottom=20),
        timestamp_seconds=3.5,
        probe=probe,
    )

    assert ok is True
    assert cover_path.exists()
    # Crop filter must match the reel framing: (W-l-r) x (H-t-b) at (l, t).
    command = captured["command"]
    assert "crop=180:360:10:20" in command
    assert "3.500" in command  # timestamp passed to -ss


def test_generate_cover_writes_ranked_candidates(monkeypatch, tmp_path: Path) -> None:
    probe = VideoProbe(width=200, height=400, duration_seconds=10.0)
    monkeypatch.setattr(thumbnail, "probe_video", lambda _p: probe)
    monkeypatch.setattr(thumbnail, "resolve_cover_crop", lambda *_a, **_k: CropSettings())

    def fake_rank(_source_path, _probe, *, crop, sample_count, penalize_text=True):
        return [(5.0, 50.0), (2.0, 20.0), (8.0, 10.0)]

    captured: list[tuple[Path, float]] = []

    def fake_extract(*, source_path, cover_path, crop, timestamp_seconds, probe=None):
        captured.append((Path(cover_path), timestamp_seconds))
        Path(cover_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cover_path).write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(thumbnail, "_rank_cover_timestamps", fake_rank)
    monkeypatch.setattr(thumbnail, "extract_cover_at", fake_extract)

    result = thumbnail.generate_cover(
        source_path=tmp_path / "clip.mp4",
        output_video_path=tmp_path / "reel_001.mp4",
        crop=CropSettings(),
        candidate_count=2,
    )

    assert result is not None
    assert result.image_path == tmp_path / "covers" / "reel_001_cover.jpg"
    assert result.timestamp_seconds == 5.0
    assert [candidate.timestamp_seconds for candidate in result.candidates] == [5.0, 2.0]
    assert [candidate.image_path.name for candidate in result.candidates] == [
        "reel_001_cover_candidate_01.jpg",
        "reel_001_cover_candidate_02.jpg",
    ]
    assert captured == [
        (tmp_path / "covers" / "reel_001_cover.jpg", 5.0),
        (tmp_path / "covers" / "reel_001_cover_candidate_01.jpg", 5.0),
        (tmp_path / "covers" / "reel_001_cover_candidate_02.jpg", 2.0),
    ]


def test_generate_cover_hook_strategy_samples_the_output_reel(monkeypatch, tmp_path: Path) -> None:
    """Default 'hook' strategy: cover comes from the finished reel (hook visible),
    text penalty off, no content crop."""
    probe = VideoProbe(width=200, height=400, duration_seconds=10.0)
    monkeypatch.setattr(thumbnail, "probe_video", lambda _p: probe)

    seen: dict = {}

    def fake_rank(video_path, _probe, *, crop, sample_count, penalize_text=True):
        seen["video"] = Path(video_path)
        seen["penalize_text"] = penalize_text
        seen["crop"] = crop
        return [(4.0, 99.0)]

    monkeypatch.setattr(thumbnail, "_rank_cover_timestamps", fake_rank)
    monkeypatch.setattr(
        thumbnail,
        "extract_cover_at",
        lambda **k: (Path(k["cover_path"]).parent.mkdir(parents=True, exist_ok=True)
                     or Path(k["cover_path"]).write_bytes(b"j") or True),
    )

    result = thumbnail.generate_cover(
        source_path=tmp_path / "clip.mp4",
        output_video_path=tmp_path / "reel_001.mp4",
        crop=CropSettings(left=5, top=5, right=5, bottom=5),
        candidate_count=0,
    )

    assert result is not None
    assert seen["video"] == tmp_path / "reel_001.mp4"  # sampled the OUTPUT, not source
    assert seen["penalize_text"] is False  # hook text is wanted
    assert seen["crop"] == CropSettings()  # reel already framed, no extra crop


def test_face_bonus_is_one_without_faces(monkeypatch) -> None:
    monkeypatch.setattr(thumbnail, "_detect_faces", lambda _f: [])
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert thumbnail._face_bonus(frame) == 1.0


def test_face_bonus_rewards_large_centred_face(monkeypatch) -> None:
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    # Big centred face vs tiny corner face.
    monkeypatch.setattr(thumbnail, "_detect_faces", lambda _f: [(30, 30, 40, 40)])
    centred = thumbnail._face_bonus(frame)
    monkeypatch.setattr(thumbnail, "_detect_faces", lambda _f: [(0, 0, 10, 10)])
    corner = thumbnail._face_bonus(frame)

    assert centred > corner > 1.0
    assert centred <= 1.0 + thumbnail.FACE_BONUS_MAX


def test_face_bonus_falls_back_when_opencv_unavailable(monkeypatch) -> None:
    # Simulate OpenCV missing: the cascade is None, so detection returns [].
    monkeypatch.setattr(thumbnail, "_face_cascade", lambda: None)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert thumbnail._face_bonus(frame) == 1.0  # no crash, no bonus


def test_pick_cover_timestamp_prefers_frame_with_face(monkeypatch) -> None:
    probe = VideoProbe(width=100, height=100, duration_seconds=10.0)
    monkeypatch.setattr(thumbnail, "probe_video", lambda _p: probe)

    # Identical base quality for every frame; the frame value encodes its second.
    def fake_loader(_path, timestamp):
        return np.full((20, 20, 3), int(round(timestamp)) % 256, dtype=np.uint8)

    monkeypatch.setattr(thumbnail, "_load_video_frame_at", fake_loader)
    monkeypatch.setattr(thumbnail, "_score_frame", lambda _f: 1.0)
    # Only the ~5s frame "has a face".
    monkeypatch.setattr(thumbnail, "_face_bonus", lambda f: 3.0 if int(f[0, 0, 0]) == 5 else 1.0)

    chosen = thumbnail.pick_cover_timestamp(Path("clip.mp4"), sample_count=21)
    assert 4.0 < chosen < 6.0


def test_generate_cover_returns_none_when_ffmpeg_missing(monkeypatch, tmp_path: Path) -> None:
    probe = VideoProbe(width=200, height=400, duration_seconds=10.0)
    monkeypatch.setattr(thumbnail, "probe_video", lambda _p: probe)
    monkeypatch.setattr(thumbnail, "_load_video_frame_at", lambda *_a: _checkerboard(50.0))
    monkeypatch.setattr(thumbnail, "ffmpeg_binary", lambda: None)  # no ffmpeg => no cover

    result = thumbnail.generate_cover(
        source_path=tmp_path / "clip.mp4",
        output_video_path=tmp_path / "reel_001.mp4",
        crop=CropSettings(),
    )
    assert result is None
