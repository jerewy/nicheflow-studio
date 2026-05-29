"""Debug OCR output for each corner of a video — shows raw Tesseract findings."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nicheflow_studio.core.media_tools import ffmpeg_binary, tesseract_binary
from nicheflow_studio.processing.video import (
    _parse_tesseract_text_boxes,
    _run_tesseract_tsv,
    _sample_timestamps,
    probe_video,
)
from nicheflow_studio.processing.watermark import (
    _corner_dimensions,
    _corner_offsets,
    _extract_corner_crop,
)

URL = "https://www.instagram.com/p/DYmYwqDoNFK/"

tmp = PROJECT_ROOT / "tmp_probe"
tmp.mkdir(exist_ok=True)
out = tmp / "test_debug.mp4"

if not out.exists():
    print("Downloading...")
    subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--quiet", "-o", str(out),
         "--merge-output-format", "mp4", URL],
        check=True,
    )

print(f"File      : {out.name} ({out.stat().st_size // 1024} KB)")
probe = probe_video(out)
print(f"Dimensions: {probe.width}x{probe.height}  duration: {probe.duration_seconds:.1f}s")

ffmpeg_path = ffmpeg_binary()
tesseract_path = tesseract_binary()
timestamps = _sample_timestamps(probe, count=3)
cw, ch = _corner_dimensions(probe)
print(f"Timestamps: {[round(t, 2) for t in timestamps]}")
print(f"Corner crop: {cw}x{ch}")
print()

for corner in ("top-left", "top-right", "bottom-left", "bottom-right"):
    x_off, y_off = _corner_offsets(probe, corner)
    print(f"Corner {corner}  (offset {x_off},{y_off}):")
    for ts in timestamps:
        frame = tmp / f"dbg_{corner.replace('-', '_')}_{ts:.0f}.png"
        try:
            _extract_corner_crop(ffmpeg_path, out, ts, probe, corner, frame)
            tsv = _run_tesseract_tsv(tesseract_path, frame)
            boxes = _parse_tesseract_text_boxes(tsv)
            if boxes:
                for b in boxes:
                    print(f"  t={ts:.1f}s  conf={b.confidence:.0f}  [{b.text!r}]  at ({b.left},{b.top})")
            else:
                print(f"  t={ts:.1f}s  (nothing)")
        except Exception as exc:  # noqa: BLE001
            print(f"  t={ts:.1f}s  ERROR: {exc}")
    print()
