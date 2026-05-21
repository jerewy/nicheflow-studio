from __future__ import annotations

from pathlib import Path

from nicheflow_studio.core import media_tools


def test_windows_media_binary_candidates_include_tesseract_default_install_path() -> None:
    candidates = media_tools._windows_media_binary_candidates("tesseract")

    assert Path("C:/Program Files/Tesseract-OCR/tesseract.exe") in candidates


def test_windows_media_binary_candidates_ignore_non_tesseract_tools() -> None:
    assert media_tools._windows_media_binary_candidates("ffmpeg") == []
