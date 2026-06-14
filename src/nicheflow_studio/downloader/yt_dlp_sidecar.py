from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from nicheflow_studio.core.media_tools import subprocess_run_kwargs

logger = logging.getLogger(__name__)


def yt_dlp_sidecar_path() -> Path | None:
    override = os.environ.get("NICHEFLOW_YT_DLP_PATH")
    if override:
        path = Path(override).expanduser().resolve()
        return path if path.exists() else None

    if not getattr(sys, "frozen", False):
        return None

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    path = bundle_root / "yt-dlp.exe"
    return path if path.exists() else None


def start_sidecar_update() -> threading.Thread | None:
    """Run the standalone updater without delaying packaged app startup."""
    sidecar = yt_dlp_sidecar_path()
    if sidecar is None:
        return None
    thread = threading.Thread(
        target=_update_sidecar,
        args=(sidecar,),
        name="nicheflow-yt-dlp-update",
        daemon=True,
    )
    thread.start()
    return thread


def _update_sidecar(sidecar: Path) -> None:
    try:
        result = subprocess.run(
            [str(sidecar), "-U"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            **subprocess_run_kwargs(),
        )
    except Exception:
        logger.warning("yt-dlp sidecar update check failed; continuing with bundled version.", exc_info=True)
        return
    if result.returncode != 0:
        logger.warning("yt-dlp sidecar update check failed: %s", result.stderr.strip())
    else:
        logger.info("yt-dlp sidecar update check: %s", result.stdout.strip())


def download_with_sidecar(
    *,
    sidecar: Path,
    url: str,
    output_dir: Path,
    format_selector: str,
    merge_output_format: str | None = None,
    cookiefile: str | None = None,
) -> tuple[dict[str, object], Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = output_dir / "%(extractor)s_%(id)s_%(title).80s.%(ext)s"
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="nicheflow-yt-dlp-",
        dir=output_dir,
        delete=False,
    ) as path_file:
        path_file_path = Path(path_file.name)

    command = [
        str(sidecar),
        "--no-playlist",
        "--restrict-filenames",
        "--windows-filenames",
        "--quiet",
        "--no-warnings",
        "--print-json",
        "--no-simulate",
        "--print-to-file",
        "after_move:filepath",
        str(path_file_path),
        "--output",
        str(output_template),
        "--format",
        format_selector,
    ]
    if merge_output_format is not None:
        command.extend(["--merge-output-format", merge_output_format])
    if cookiefile:
        command.extend(["--cookies", cookiefile])
    command.append(url)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            **subprocess_run_kwargs(),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "yt-dlp sidecar download failed")
        output_lines = [line for line in result.stdout.splitlines() if line.strip()]
        info = json.loads(output_lines[-1]) if output_lines else {}
        file_lines = [
            line.strip()
            for line in path_file_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not file_lines:
            raise RuntimeError("yt-dlp sidecar did not report the downloaded file path")
        file_path = Path(file_lines[-1])
        if not file_path.is_file():
            raise RuntimeError(f"yt-dlp sidecar did not produce the expected file: {file_path}")
        return info, file_path
    finally:
        path_file_path.unlink(missing_ok=True)
