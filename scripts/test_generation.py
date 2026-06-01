"""
Batch generation test script.

Usage:
    $env:GROQ_API_KEY = "gsk_..."
    .venv/Scripts/python.exe scripts/test_generation.py

Optional filters:
    --account 4             only process items for account id 4
    --limit 5               stop after 5 items
    --profile gaming_meme   force a prompt profile
    --no-regen              skip items that already have drafts
    --require-vision        fail low-context items where vision didn't run
                            instead of accepting writer-only output
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

# Generated titles/captions (and this script's box-drawing separators) contain
# non-cp1252 characters like the em-dash. A default Windows console is cp1252, so
# printing them raises UnicodeEncodeError and hides the actual output. Force the
# stream to UTF-8 so the smoke is readable on a stock PowerShell/cmd window.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

# Load a .env file from the repo root if present (simple key=value parser, no extra deps)
_env_file = pathlib.Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

import nicheflow_studio.db.session as db_session
from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.processing.smart_drafts import (
    VisionRequiredError,
    generate_smart_drafts,
)

# ---------------------------------------------------------------------------
# Account voice presets — edit these to match what the account should sound like
# ---------------------------------------------------------------------------
ACCOUNT_VOICE_PRESETS: dict[int, dict[str, str]] = {
    # "Test IG" / meme.ig style
    4: {
        "niche": "meme clips and relatable reaction videos",
        "tone": "funny",
        "hook_style": "situational POV",
        "writing_tone": "casual and punchy",
        "target_audience": "Gen Z gamers and meme fans",
        "banned_phrases": "master of rhymes,epic gameplay,in today's video",
    },
    # "test 2" — Minecraft gaming
    2: {
        "niche": "Minecraft gaming highlights and trapping moments",
        "tone": "hype",
        "hook_style": "reaction moment",
        "writing_tone": "energetic gaming",
        "target_audience": "Minecraft players aged 13-25",
        "banned_phrases": "",
    },
    # "YT Main"
    3: {
        "niche": "general short-form clips",
        "tone": "question",
        "hook_style": "relatable setup",
        "writing_tone": "clean and punchy",
        "target_audience": "broad short-form audience",
        "banned_phrases": "",
    },
}

PROFILE_FOR_ACCOUNT: dict[int, str] = {
    4: "gaming_meme",
    2: "gaming_meme",
    3: "broad_short_form",
}


def _separator(char: str = "─", width: int = 70) -> str:
    return char * width


def _print_result(item: DownloadItem, drafts, profile: str) -> None:
    print(_separator("═"))
    print(f"Item [{item.id}] | account={item.account_id} | profile={profile}")
    print(f"Source title : {item.title or '(none)'}")
    print(f"Provider     : {drafts.provider_label}")
    _print_generation_diagnostics(item, drafts)
    print()


def _print_generation_diagnostics(item: DownloadItem, drafts) -> None:
    """Surface vision_attempted / vision_used / vision_error / frame_count.

    This is the visibility Fix A is meant to give: when a real-clip run uses
    writer-only Groq because vision silently failed, the operator now sees
    *why* instead of only seeing a generic hook.
    """
    meta = drafts.generation_meta or {}
    frame_count = meta.get("frame_count", 0)
    vision_attempted = meta.get("vision_attempted", False)
    vision_used = meta.get("vision_used", False)
    vision_retry = meta.get("vision_retry_attempted", False)
    low_context = meta.get("low_context", False)
    vision_error = meta.get("vision_error")
    print(
        "Diagnostics  : "
        f"frames={frame_count} "
        f"vision_attempted={vision_attempted} "
        f"vision_used={vision_used} "
        f"retry={vision_retry} "
        f"low_context={low_context}"
    )
    # Print the error whenever the writer ran without vision grounding on a
    # clip that DID have an input file — that's the "silent vision failure"
    # case Fix A is built to expose.
    had_input = bool(item.file_path)
    if had_input and not vision_used and vision_error:
        print(f"Vision error : {vision_error}")

    if drafts.vision_payload:
        vp = drafts.vision_payload
        print(f"Vision: {vp.get('scene_summary','')[:100]}")
        hook = vp.get("on_screen_hook","")
        if hook:
            print(f"  on_screen_hook     : {hook}")
        entity = vp.get("referenced_entity","")
        if entity:
            print(f"  referenced_entity  : {entity}")
        premise = vp.get("meme_caption_premise","")
        if premise:
            print(f"  meme_caption_premise: {premise}")
        top_ratio = vp.get("suggested_top_crop_ratio", 0.0)
        layout = vp.get("suggested_title_layout","?")
        print(f"  layout={layout}  top_crop_ratio={top_ratio:.2f}")
        print()

    print("TITLE OPTIONS:")
    for i, t in enumerate(drafts.title_options, 1):
        print(f"  {i}. {t}")
    print()
    print("CAPTION OPTIONS:")
    for i, c in enumerate(drafts.caption_options, 1):
        lines = c.split("\n")
        print(f"  {i}.")
        for line in lines:
            print(f"     {line}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", type=int, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--no-regen", action="store_true")
    parser.add_argument(
        "--require-vision",
        action="store_true",
        help=(
            "Fail low-context items (generic title + no transcript) instead of "
            "accepting writer-only output. Use this to validate that vision is "
            "actually running on clips where it matters."
        ),
    )
    args = parser.parse_args()

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY not set. Run: $env:GROQ_API_KEY = 'gsk_...'")
        print(f"  .env path checked: {_env_file}")
        print(f"  .env exists: {_env_file.exists()}")
        sys.exit(1)
    print(f"API key loaded: {api_key[:8]}... ({len(api_key)} chars)")

    db_session.init_db()

    with db_session.get_session() as session:
        query = session.query(DownloadItem).filter(DownloadItem.status == "downloaded")
        if args.account is not None:
            query = query.filter(DownloadItem.account_id == args.account)
        items = query.order_by(DownloadItem.id).all()

    processed = 0
    errors = 0
    vision_required_failures = 0
    for item in items:
        if args.no_regen and item.smart_title_options:
            continue
        if not item.file_path or not pathlib.Path(item.file_path).exists():
            print(f"[{item.id}] skipping — file missing")
            continue
        if processed >= args.limit:
            break

        account_id = item.account_id or 0
        voice_preset = ACCOUNT_VOICE_PRESETS.get(account_id, {})
        profile = args.profile or PROFILE_FOR_ACCOUNT.get(account_id, "broad_short_form")

        account_voice: dict[str, str] = {}
        for key in ("niche", "tone", "hook_style", "writing_tone", "target_audience", "banned_phrases"):
            if voice_preset.get(key):
                account_voice[key] = voice_preset[key]

        print(f"\nGenerating [{item.id}] {(item.title or '')[:60]}...")
        try:
            drafts = generate_smart_drafts(
                transcript_text=item.transcript_text or "",
                source_title=item.title,
                niche_label=voice_preset.get("niche"),
                source_description=item.source_description,
                input_path=pathlib.Path(item.file_path),
                api_key=api_key,
                account_voice=account_voice if account_voice else None,
                prompt_profile=profile,
                require_vision=args.require_vision,
            )
            _print_result(item, drafts, profile)
            processed += 1
        except VisionRequiredError as exc:
            print(f"  VISION-REQUIRED FAILURE: {exc}")
            vision_required_failures += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  ERROR: {exc}")
            traceback.print_exc()
            errors += 1

    print(_separator("═"))
    print(
        f"Done. {processed} generated, {errors} errors, "
        f"{vision_required_failures} vision-required failures."
    )


if __name__ == "__main__":
    main()
