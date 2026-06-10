"""Codex-facing CLI for the database-backed Processing draft handoff.

This is a thin adapter over ``nicheflow_studio.services.draft_revisions``; it
contains no business rules of its own. Codex uses it to read the active
Processing context and to save, revise, apply, or inspect versioned draft
revisions in SQLite. The running desktop app (and the future React UI) read the
same revisions back without a restart.

Examples (PowerShell):

    .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py current
    .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py context --item-id 12
    .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py save --item-id 12 --file draft.json
    .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py revise --item-id 12 --option 2 --file one.json
    .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py apply --item-id 12 --option 2
    .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py history --item-id 12

Prefer ``--file`` over piping into ``--stdin``: PowerShell's ``Get-Content``
re-decodes BOM-less UTF-8 files as the ANSI codepage before the bytes reach
Python, silently corrupting em dashes and emoji. ``--file`` opens the file
directly as UTF-8 and is immune.

``save`` stdin JSON shape (only title_options + caption_options are required)::

    {
      "title_options": ["...", "...", "..."],
      "caption_options": ["...", "...", "..."],
      "summary": "one sentence about the clip",
      "option_notes": ["when to use 1", "when to use 2", "when to use 3"],
      "option_tiers": ["green", "yellow", "yellow"],
      "recommended_title_index": 1,
      "recommended_caption_index": 1,
      "recommendation_reason": "why this pair",
      "title_style_preset": "meme_setup_punchline",
      "caption_style_preset": "contextual_info",
      "provider_label": "Codex",
      "source": "codex"
    }

``revise`` stdin JSON shape::

    {"title": "new title", "caption": "new caption", "note": "new note"}
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# Generated titles/captions contain non-cp1252 characters (em-dash, emoji). A
# default Windows console is cp1252, so force UTF-8 to keep JSON output readable.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
import os

os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

from nicheflow_studio.services import draft_revisions as svc  # noqa: E402


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_payload_json(args: argparse.Namespace) -> dict:
    """Read the revision JSON from --file when given, else from stdin."""
    file_path = getattr(args, "file", None)
    if not file_path:
        return _read_stdin_json()
    path = pathlib.Path(file_path)
    if not path.is_file():
        raise svc.DraftRevisionError(f"--file not found: {path}")
    # utf-8-sig tolerates a BOM from editors/PowerShell Out-File.
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        raise svc.DraftRevisionError(f"--file is empty: {path}")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise svc.DraftRevisionError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise svc.DraftRevisionError("Top-level JSON must be an object.")
    return parsed


def _read_stdin_json() -> dict:
    # Read the raw byte buffer as utf-8-sig so we are immune to the Windows
    # console codepage (cp1252 can't decode emoji/em-dash) and to a UTF-8 BOM
    # that PowerShell prepends when piping a string into a native process.
    # Fall back to text mode for tests that patch sys.stdin with a StringIO.
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        # utf-8-sig also strips a leading BOM, which PowerShell prepends when
        # piping a string into a native process.
        raw = buffer.read().decode("utf-8-sig")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        raise svc.DraftRevisionError("Expected JSON on stdin but received nothing.")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise svc.DraftRevisionError(f"Invalid JSON on stdin: {exc}") from exc
    if not isinstance(parsed, dict):
        raise svc.DraftRevisionError("Top-level stdin JSON must be an object.")
    return parsed


def _cmd_current(_args: argparse.Namespace) -> None:
    _emit(svc.active_context(None))


def _cmd_context(args: argparse.Namespace) -> None:
    _emit(svc.active_context(args.item_id))


def _cmd_save(args: argparse.Namespace) -> None:
    payload = _read_payload_json(args)
    dto = svc.save_revision(
        args.item_id,
        title_options=payload.get("title_options") or [],
        caption_options=payload.get("caption_options") or [],
        summary=payload.get("summary"),
        option_notes=payload.get("option_notes"),
        option_tiers=payload.get("option_tiers"),
        recommended_title_index=payload.get("recommended_title_index"),
        recommended_caption_index=payload.get("recommended_caption_index"),
        recommendation_reason=payload.get("recommendation_reason"),
        title_style_preset=payload.get("title_style_preset"),
        caption_style_preset=payload.get("caption_style_preset"),
        provider_label=payload.get("provider_label"),
        generation_meta=payload.get("generation_meta"),
        vision_payload=payload.get("vision_payload"),
        source=payload.get("source") or "codex",
    )
    _emit(dto.to_dict())


def _cmd_revise(args: argparse.Namespace) -> None:
    payload = _read_payload_json(args)
    dto = svc.revise_option(
        args.item_id,
        args.option,
        title=payload.get("title"),
        caption=payload.get("caption"),
        note=payload.get("note"),
        revision_id=payload.get("revision_id", args.revision_id),
        source=payload.get("source") or "codex",
    )
    _emit(dto.to_dict())


def _cmd_apply(args: argparse.Namespace) -> None:
    _emit(svc.apply_revision(args.item_id, args.option, revision_id=args.revision_id))


def _cmd_history(args: argparse.Namespace) -> None:
    _emit([dto.to_dict() for dto in svc.list_revisions(args.item_id)])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_current = sub.add_parser("current", help="Print the active Processing context.")
    p_current.set_defaults(func=_cmd_current)

    p_context = sub.add_parser("context", help="Print context for a specific item.")
    p_context.add_argument("--item-id", type=int, required=True)
    p_context.set_defaults(func=_cmd_context)

    p_save = sub.add_parser("save", help="Save a new draft revision from stdin JSON.")
    p_save.add_argument("--item-id", type=int, required=True)
    p_save.add_argument("--stdin", action="store_true", help="Read revision JSON from stdin.")
    p_save.add_argument(
        "--file",
        type=str,
        default=None,
        help="Read revision JSON from a UTF-8 file (preferred over piping into --stdin).",
    )
    p_save.set_defaults(func=_cmd_save)

    p_revise = sub.add_parser("revise", help="Revise one option into a new revision.")
    p_revise.add_argument("--item-id", type=int, required=True)
    p_revise.add_argument("--option", type=int, required=True, help="1-based option number.")
    p_revise.add_argument(
        "--revision-id", type=int, default=None, help="Base revision (default: latest)."
    )
    p_revise.add_argument(
        "--stdin", action="store_true", help="Read {title,caption,note} from stdin."
    )
    p_revise.add_argument(
        "--file",
        type=str,
        default=None,
        help="Read {title,caption,note} JSON from a UTF-8 file (preferred over --stdin).",
    )
    p_revise.set_defaults(func=_cmd_revise)

    p_apply = sub.add_parser("apply", help="Apply one option to the item's final draft.")
    p_apply.add_argument("--item-id", type=int, required=True)
    p_apply.add_argument("--option", type=int, required=True, help="1-based option number.")
    p_apply.add_argument(
        "--revision-id", type=int, default=None, help="Source revision (default: latest)."
    )
    p_apply.set_defaults(func=_cmd_apply)

    p_history = sub.add_parser("history", help="List all revisions for an item.")
    p_history.add_argument("--item-id", type=int, required=True)
    p_history.set_defaults(func=_cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except svc.DraftRevisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
