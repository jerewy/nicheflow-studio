"""Quick probe: download Instagram clips and test watermark detection + cover.

Usage:
    python scripts/watermark_probe.py --url URL [--url URL ...]
    python scripts/watermark_probe.py --url URL --cover --brand "@meme.ig"
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nicheflow_studio.core.media_tools import ffmpeg_binary, tesseract_binary
from nicheflow_studio.processing.watermark import cover_watermark, detect_watermark


def _download_video(url: str, output_dir: Path) -> Path | None:
    """Download a single Instagram URL with yt-dlp, return the video file path."""
    import subprocess

    output_dir.mkdir(parents=True, exist_ok=True)
    template = str(output_dir / "%(id)s.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--quiet",
        "--no-warnings",
        "-o", template,
        "--merge-output-format", "mp4",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  yt-dlp error: {result.stderr.strip()[:200]}")
        return None

    # Find the downloaded file
    mp4s = sorted(output_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if mp4s:
        return mp4s[0]
    # Fallback: any video file
    for ext in ("mp4", "mkv", "webm"):
        files = sorted(output_dir.glob(f"*.{ext}"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Test watermark detection on Instagram URLs.")
    parser.add_argument("--url", action="append", default=[], help="Instagram URL")
    parser.add_argument("--cover", action="store_true", help="Apply cover if watermark found")
    parser.add_argument("--brand", default="@meme.ig", help="Replacement text (default: @meme.ig)")
    parser.add_argument("--keep", action="store_true", help="Keep downloaded files after probe")
    args = parser.parse_args()

    if not args.url:
        parser.error("Provide at least one --url")

    print("-" * 60)
    print("Tool availability")
    print("-" * 60)
    print(f"  FFmpeg    : {'found' if ffmpeg_binary() else 'NOT FOUND'}")
    print(f"  Tesseract : {'found' if tesseract_binary() else 'NOT FOUND'}")
    print()

    with tempfile.TemporaryDirectory(prefix="nicheflow-wm-probe-") as tmp:
        tmp_path = Path(tmp)

        for idx, url in enumerate(args.url):
            print("-" * 60)
            print(f"[{idx + 1}/{len(args.url)}] {url}")
            print("-" * 60)

            print("  Downloading...")
            video_path = _download_video(url, tmp_path / f"vid{idx}")
            if video_path is None:
                print("  FAILED — could not download video")
                print()
                continue
            print(f"  Downloaded : {video_path.name} ({video_path.stat().st_size // 1024} KB)")

            print("  Detecting watermark...")
            region = detect_watermark(video_path)

            if region is None:
                print("  Result     : no @handle watermark detected")
            else:
                print(f"  Result     : FOUND")
                print(f"  Handle     : {region.text}")
                print(f"  Corner     : {region.corner}")
                print(f"  Position   : x={region.x} y={region.y} w={region.w} h={region.h}")

                if args.cover:
                    print(f"  Covering with '{args.brand}'...")
                    if args.keep:
                        out_path = video_path.parent / (video_path.stem + "_branded.mp4")
                    else:
                        out_path = None
                    try:
                        result = cover_watermark(
                            video_path,
                            region,
                            replacement_text=args.brand,
                            output_path=out_path,
                        )
                        size_kb = result.output_path.stat().st_size // 1024
                        print(f"  Cover done : {result.output_path.name} ({size_kb} KB)")
                        if args.keep:
                            # Copy to project tmp_probe dir so it survives cleanup
                            import shutil
                            out_dir = PROJECT_ROOT / "tmp_probe"
                            out_dir.mkdir(exist_ok=True)
                            dest = out_dir / result.output_path.name
                            shutil.copy2(result.output_path, dest)
                            print(f"  Saved to   : {dest}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  Cover FAILED: {exc}")

            print()

    print("-" * 60)
    print("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
