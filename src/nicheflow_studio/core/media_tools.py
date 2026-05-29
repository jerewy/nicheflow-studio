from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path


@functools.lru_cache(maxsize=None)
def find_media_binary(name: str) -> Path | None:
    direct_match = shutil.which(name)
    if direct_match:
        return Path(direct_match).resolve()

    if os.name != "nt":
        return None

    for candidate in _windows_media_binary_candidates(name):
        if candidate.exists():
            return candidate.resolve()

    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None

    packages_root = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
    if not packages_root.exists():
        return None

    pattern = f"**/{name}.exe"
    candidates = sorted(
        packages_root.glob(pattern),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0].resolve() if candidates else None


def _windows_media_binary_candidates(name: str) -> list[Path]:
    if name != "tesseract":
        return []
    return [
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    ]


def ffmpeg_binary() -> Path | None:
    return find_media_binary("ffmpeg")


def ffprobe_binary() -> Path | None:
    return find_media_binary("ffprobe")


def tesseract_binary() -> Path | None:
    return find_media_binary("tesseract")


def subprocess_run_kwargs() -> dict[str, int]:
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def windows_emoji_font_file() -> Path | None:
    """Path to Segoe UI Emoji (``seguiemj.ttf``) when available.

    Pillow uses this font with ``embedded_color=True`` to render color emoji
    glyphs into the overlay title PNG. Returns None on non-Windows or when
    the file is missing — callers fall back to stripping emoji from the
    title rather than shipping missing-glyph boxes.
    """
    if os.name != "nt":
        return None
    windows_dir = Path(os.environ.get("WINDIR", "C:/Windows"))
    candidate = windows_dir / "Fonts" / "seguiemj.ttf"
    if candidate.exists():
        return candidate.resolve()
    return None


def windows_font_file(font_name: str | None = None) -> Path | None:
    if os.name != "nt":
        return None
    windows_dir = Path(os.environ.get("WINDIR", "C:/Windows"))
    font_map = {
        "arial_black": "ariblk.ttf",
        "segoe_ui": "segoeui.ttf",
        "bahnschrift": "bahnschrift.ttf",
        "arial_bold": "arialbd.ttf",
        "arial_rounded_bold": "ARLRDBD.TTF",
        "impact": "impact.ttf",
        "georgia": "georgia.ttf",
        "georgia_italic": "georgiai.ttf",
        "georgia_bold": "georgiab.ttf",
        "times_italic": "timesi.ttf",
        "comic_italic": "comici.ttf",
        "comic_bold": "comicbd.ttf",
        "lilita_one_style": "comicbd.ttf",
        "grobold_style": "impact.ttf",
        "arial": "arial.ttf",
    }
    candidates: list[Path] = []
    if font_name:
        mapped_name = font_map.get(font_name, font_name)
        candidates.append(windows_dir / "Fonts" / mapped_name)
    candidates.extend(
        [
            # Prefer bold chunky fonts for watermark replacement text.
            windows_dir / "Fonts" / "ariblk.ttf",   # Arial Black — chunky, wide
            windows_dir / "Fonts" / "arialbd.ttf",  # Arial Bold — fallback
            windows_dir / "Fonts" / "bahnschrift.ttf",
            windows_dir / "Fonts" / "segoeui.ttf",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None
