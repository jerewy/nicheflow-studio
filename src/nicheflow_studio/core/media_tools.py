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


def windows_font_file(font_name: str | None = None) -> Path | None:
    if os.name != "nt":
        return None
    windows_dir = Path(os.environ.get("WINDIR", "C:/Windows"))
    font_map = {
        "segoe_ui": "segoeui.ttf",
        "bahnschrift": "bahnschrift.ttf",
        "arial_bold": "arialbd.ttf",
        "impact": "impact.ttf",
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
            windows_dir / "Fonts" / "segoeui.ttf",
            windows_dir / "Fonts" / "comicbd.ttf",
            windows_dir / "Fonts" / "impact.ttf",
            windows_dir / "Fonts" / "arialbd.ttf",
            windows_dir / "Fonts" / "arial.ttf",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None
