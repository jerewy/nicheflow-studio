from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

try:
    from instagram_inject_cookies import DEFAULT_COOKIES_PATH, inject_cookies
    from instagram_inject_cookies import DEFAULT_STORAGE_STATE_PATH
    from instagram_inject_cookies import normalize_cookie_editor_export
except ModuleNotFoundError:
    from scripts.instagram_inject_cookies import DEFAULT_COOKIES_PATH, inject_cookies
    from scripts.instagram_inject_cookies import DEFAULT_STORAGE_STATE_PATH
    from scripts.instagram_inject_cookies import normalize_cookie_editor_export


def _read_clipboard_with_tkinter() -> str | None:
    try:
        import tkinter as tk
    except Exception:
        return None

    root = tk.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    except Exception:
        return None
    finally:
        root.destroy()


def _read_clipboard_with_powershell() -> str | None:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return None

    if completed.returncode != 0:
        return None
    return completed.stdout


def read_clipboard() -> str:
    text = _read_clipboard_with_tkinter() or _read_clipboard_with_powershell()
    if text is None or not text.strip():
        raise ValueError("Clipboard is empty or could not be read.")
    return text


def _read_cookie_json(*, from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.read()
    return read_clipboard()


def save_cookie_export(*, raw_json: str, output_path: Path) -> Path:
    loaded = json.loads(raw_json)
    cookies = normalize_cookie_editor_export(loaded)
    cookie_names = {cookie["name"] for cookie in cookies}
    if "sessionid" not in cookie_names:
        raise ValueError(
            "Cookie export does not include sessionid. "
            "Log into instagram.com in Chrome/Edge, then export instagram.com cookies again."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(loaded, indent=2), encoding="utf-8")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Save a Cookie-Editor Instagram JSON export from the clipboard or stdin, "
            "then optionally inject it into Playwright storage state."
        )
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_COOKIES_PATH,
        help=f"Where to save the cookie export (default: {DEFAULT_COOKIES_PATH})",
    )
    parser.add_argument(
        "--from-stdin",
        action="store_true",
        help="Read the cookie JSON from stdin instead of the clipboard",
    )
    parser.add_argument(
        "--inject",
        action="store_true",
        help="After saving, validate and write Playwright storage-state.json",
    )
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=DEFAULT_STORAGE_STATE_PATH,
        help=f"Storage state path used with --inject (default: {DEFAULT_STORAGE_STATE_PATH})",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        saved_path = save_cookie_export(
            raw_json=_read_cookie_json(from_stdin=args.from_stdin),
            output_path=args.out,
        )
    except json.JSONDecodeError as exc:
        print(f"Cookie export is not valid JSON: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Saved Instagram cookie export to: {saved_path}")
    print("Cookie values were not printed.")

    if args.inject:
        return asyncio.run(
            inject_cookies(cookies_path=saved_path, storage_state_path=args.storage_state)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
