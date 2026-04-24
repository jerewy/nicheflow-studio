from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nicheflow_studio.processing import transcription


def test_generate_transcript_draft_builds_title_and_caption(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(transcription, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))

    class FakeSegment:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeModel:
        def __init__(self, model_size: str, device: str, compute_type: str) -> None:
            assert model_size == "base"
            assert device == "cpu"
            assert compute_type == "int8"

        def transcribe(self, audio_path: str, vad_filter: bool = True):
            assert audio_path.endswith("audio.wav")
            assert vad_filter is True
            return iter(
                [
                    FakeSegment("This is the main message of the clip."),
                    FakeSegment("Here is a second supporting sentence."),
                ]
            ), {}

    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", type("M", (), {"WhisperModel": FakeModel})())

    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["creationflags"] = kwargs.get("creationflags")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transcription.subprocess, "run", fake_run)

    draft = transcription.generate_transcript_draft(input_path, fallback_title="Fallback title")

    assert draft.transcript_text.startswith("This is the main message")
    assert draft.title_draft == "This is the main message of the clip"
    assert "second supporting sentence" in draft.caption_draft
    assert captured["command"][0] == str(Path("C:/tools/ffmpeg.exe"))
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_generate_transcript_draft_requires_dependency(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(transcription, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))

    original_import = __import__

    def fake_import(name, *args, **kwargs):  # noqa: ANN001
        if name == "faster_whisper":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError, match="faster-whisper is not installed"):
        transcription.generate_transcript_draft(input_path)


def test_generate_transcript_draft_in_subprocess_parses_json(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["creationflags"] = kwargs.get("creationflags")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"transcript_text":"hello world","title_draft":"hello","caption_draft":"world"}',
            stderr="",
        )

    monkeypatch.setattr(transcription.subprocess, "run", fake_run)

    draft = transcription.generate_transcript_draft_in_subprocess(
        input_path,
        fallback_title="fallback",
        python_executable="python-test",
    )

    assert draft.transcript_text == "hello world"
    assert draft.title_draft == "hello"
    assert draft.caption_draft == "world"
    command = captured["command"]
    assert command[0] == "python-test"
    assert command[1:3] == ["-m", "nicheflow_studio.processing.transcription"]
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_generate_transcript_draft_in_subprocess_raises_on_failure(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(transcription.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="boom"):
        transcription.generate_transcript_draft_in_subprocess(input_path)
