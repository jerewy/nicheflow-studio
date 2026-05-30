r"""CLI for the Instagram Reel publisher (thin wrapper over the publisher module).

The real logic lives in ``nicheflow_studio.publisher.instagram_publisher``; this
script just parses args and maps the result to an exit code, so the CLI and the
app share one implementation.

Usage (PowerShell):
    # Safe dry run: drives the whole flow but stops BEFORE the final Share.
    .\.venv\Scripts\python.exe scripts/instagram_publish_reel.py `
        --profile main --video "data/processed/some-reel.mp4" `
        --caption-file caption.txt --no-share --keep-open

    # Real post:
    .\.venv\Scripts\python.exe scripts/instagram_publish_reel.py `
        --profile main --video "data/processed/some-reel.mp4" --caption "gg #gaming"

Exit codes:
    0 - reel shared (or dry run reached Share with --no-share)
    1 - flow failed at a known step
    2 - profile not logged in / session expired
    3 - Instagram checkpoint/challenge detected
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nicheflow_studio.core.instagram_session import DEFAULT_PROFILE_NAME
from nicheflow_studio.publisher.instagram_publisher import publish_reel


def _resolve_caption(args: argparse.Namespace) -> str:
    if args.caption_file:
        return Path(args.caption_file).read_text(encoding="utf-8").strip()
    return (args.caption or "").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish an Instagram Reel via the saved profile.")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_NAME,
                        help=f"Saved profile name (default: {DEFAULT_PROFILE_NAME}).")
    parser.add_argument("--video", required=True, type=Path, help="Path to the vertical MP4 reel.")
    parser.add_argument("--caption", default="", help="Caption text (or use --caption-file).")
    parser.add_argument("--caption-file", default=None,
                        help="Read caption from a UTF-8 file (avoids shell escaping issues).")
    parser.add_argument("--no-share", action="store_true",
                        help="Drive the whole flow but stop before the final Share.")
    parser.add_argument("--keep-open", action="store_true",
                        help="Leave the browser open at the end / on failure for inspection.")
    parser.add_argument("--channel", default="chrome",
                        help="Browser channel (default: chrome). Pass '' to use bundled Chromium.")
    parser.add_argument("--upload-timeout", type=float, default=180.0,
                        help="Seconds to wait for the share confirmation (default: 180).")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.video.exists():
        print(f"Video not found: {args.video}", file=sys.stderr)
        return 1
    caption = _resolve_caption(args)
    print(f"Profile: {args.profile}", file=sys.stderr)
    print(f"Video:   {args.video}", file=sys.stderr)
    print(f"Caption: {caption[:60]}{'...' if len(caption) > 60 else ''}", file=sys.stderr)

    result = publish_reel(
        args.profile,
        args.video,
        caption,
        do_share=not args.no_share,
        channel=(args.channel or None),
        upload_timeout_s=args.upload_timeout,
        keep_open=args.keep_open,
    )

    if result.status == "dry_run":
        print("DRY RUN: reached the Share step. Not posting (--no-share).")
        return 0
    if result.status == "posted":
        print(f"SUCCESS: reel shared. url={result.posted_url or '(not captured)'}")
        return 0
    if result.status == "checkpoint":
        print(f"CHECKPOINT: {result.error_message}", file=sys.stderr)
        return 3
    if result.error_message and "re-login" in result.error_message:
        print(f"NOT LOGGED IN: {result.error_message}", file=sys.stderr)
        return 2
    print(f"FAILED: {result.error_message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
