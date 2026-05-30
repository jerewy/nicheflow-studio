"""Unit tests for the randomized audio-alteration helpers in processing.video.

These cover the pure logic (filter string construction + bounded randomness)
without invoking ffmpeg, so they run fast and deterministically. Actually
exporting a clip with ``audio_mode="alter"`` is a manual/integration step that
requires ffmpeg and a sample video.
"""

from __future__ import annotations

import random

from nicheflow_studio.processing.video import (
    AUDIO_ALTER_DELAY_MS_RANGE,
    AUDIO_ALTER_PITCH_RANGE,
    AUDIO_ALTER_TEMPO_RANGE,
    AUDIO_ALTER_VOLUME_RANGE,
    AudioAlterParams,
    build_audio_filter,
    random_audio_alter_params,
)


def test_default_params_produce_transparent_filter():
    # An all-default (no-op) alteration must not change the audio.
    assert build_audio_filter(AudioAlterParams()) == "anull"


def test_filter_includes_all_requested_components():
    params = AudioAlterParams(tempo=1.02, pitch=1.01, delay_ms=40, volume=1.02)

    audio_filter = build_audio_filter(params, sample_rate=44100)

    assert "asetrate=44541" in audio_filter  # 44100 * 1.01
    assert "aresample=44100" in audio_filter
    assert "atempo=" in audio_filter  # net tempo = 1.02 / 1.01 != 1.0
    assert "volume=1.02" in audio_filter
    assert "adelay=40:all=1" in audio_filter


def test_equal_tempo_and_pitch_drops_redundant_atempo():
    # When tempo == pitch the net tempo is 1.0, so no atempo stage is emitted.
    audio_filter = build_audio_filter(AudioAlterParams(tempo=1.01, pitch=1.01))

    assert "asetrate=" in audio_filter
    assert "atempo=" not in audio_filter


def test_random_params_stay_within_bounds():
    rng = random.Random(123)
    for _ in range(200):
        params = random_audio_alter_params(rng)
        assert AUDIO_ALTER_TEMPO_RANGE[0] <= params.tempo <= AUDIO_ALTER_TEMPO_RANGE[1]
        assert AUDIO_ALTER_PITCH_RANGE[0] <= params.pitch <= AUDIO_ALTER_PITCH_RANGE[1]
        assert AUDIO_ALTER_DELAY_MS_RANGE[0] <= params.delay_ms <= AUDIO_ALTER_DELAY_MS_RANGE[1]
        assert AUDIO_ALTER_VOLUME_RANGE[0] <= params.volume <= AUDIO_ALTER_VOLUME_RANGE[1]


def test_random_params_differ_across_seeds():
    # Different accounts -> different copies of the same source clip.
    first = random_audio_alter_params(random.Random(1))
    second = random_audio_alter_params(random.Random(2))
    assert first != second


def test_random_params_reproducible_with_same_seed():
    # Same seed reproduces an identical copy (traceability / regeneration).
    first = random_audio_alter_params(random.Random(7))
    second = random_audio_alter_params(random.Random(7))
    assert first == second
