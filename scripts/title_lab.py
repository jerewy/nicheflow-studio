"""Bench for the experimental on-screen title prompts.

Nothing here touches the shipping draft pipeline. It gathers one clip's
evidence once, renders the same evidence under three competing rulebooks, and
then scores the replies blind so "the new one feels better" cannot be the
finding.

Usage (PowerShell):

    .venv\\Scripts\\python.exe scripts\\title_lab.py prompts --item-id 1864
    #   -> paste each prompt file into your chat model, save each reply into
    #      the run's replies\\<variant>.txt
    .venv\\Scripts\\python.exe scripts\\title_lab.py score --run <run-dir>
    #   -> fill in scorecard.md, which shows the lines shuffled and unlabelled
    .venv\\Scripts\\python.exe scripts\\title_lab.py reveal --run <run-dir>

One clip is a smoke test, not evidence: it can show the voice moved, it cannot
show the voice is better. Run ten before believing a result.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
from dataclasses import replace

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

_env_file = pathlib.Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip())

from pathlib import Path  # noqa: E402

from nicheflow_studio.core.paths import data_dir  # noqa: E402
from nicheflow_studio.db.models import Account, DownloadItem, ScrapeCandidate  # noqa: E402
from nicheflow_studio.db.session import get_session  # noqa: E402
from nicheflow_studio.processing import title_lab, video  # noqa: E402

CRITERIA = (
    "Would a real person text this line to a friend",
    "A stranger knows what they are looking at",
    "Every claim is supported by the evidence",
)
# Two hard fails, because they are different failures with different causes.
# The first is misattribution. The second was added after a run produced "I would
# let her tell me a bedtime story any day" over a swimsuit clip: it scored 3/3 on
# every positive criterion AND passed the attribution check, because it claims
# nothing and reads as a pun. Unbanning first person does not mainly risk
# claiming the footage - it gives the account a body and opinions, and pointed at
# a real person that is where the damage is.
FAIL_CRITERIA = (
    "HARD FAIL: claims the footage as the account's own",
    "HARD FAIL: not postable under the account's name (leering at, or mocking, a real person)",
)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def gather_evidence(item_id: int, *, sheet_target: Path) -> title_lab.ClipEvidence:
    """Read one item's evidence out of SQLite without writing anything back.

    The app's own batch path calls ``ensure_batch_vision_payloads``, which runs
    a vision pass and PERSISTS the result onto the item. This does not: a bench
    run should not mutate library rows. When the item already carries a vision
    payload that JSON is the visual grounding; when it does not, a contact sheet
    is rendered into the run folder for you to attach by hand.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise SystemExit(f"No download item with id {item_id}.")
        account = session.get(Account, item.account_id) if item.account_id else None
        video_id = (item.video_id or "").strip()
        hook = None
        if video_id:
            hook = (
                session.query(ScrapeCandidate.on_screen_title)
                .filter(ScrapeCandidate.video_id == video_id)
                .filter(ScrapeCandidate.on_screen_title.is_not(None))
                .limit(1)
                .scalar()
            )
        vision_json = (item.smart_vision_payload or "").strip()
        file_path = (item.file_path or "").strip()
        evidence = {
            "item_id": item.id,
            "account_name": account.name if account else None,
            "niche_label": (account.niche_label if account else None) or None,
            "scraped_caption": item.title,
            "source_url": item.source_url,
            "source_description": item.source_description,
            "on_screen_hook": " ".join((hook or "").split()) or None,
            "transcript": item.transcript_text,
            "vision_json": vision_json or None,
            "video_path": file_path or None,
        }

    contact_sheet = None
    if not vision_json and file_path:
        source = Path(file_path)
        if source.exists():
            try:
                video.build_contact_sheet(source, sheet_target)
                contact_sheet = str(sheet_target)
            except Exception as exc:  # noqa: BLE001 - ffmpeg failures are not fatal here
                print(f"  contact sheet failed ({exc}); the prompts will run text-only")

    return title_lab.ClipEvidence(**evidence, contact_sheet=contact_sheet)


def _account_style_lines(item_id: int) -> str | None:
    """The account's writing_tone / hook_style, formatted as production does.

    Only used by the A/B that tests whether those two settings pull against the
    v2 rules. Formatted to match ``build_chat_prompt``'s account block exactly,
    because a paraphrase would test my wording rather than what production
    actually sends.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id) if item and item.account_id else None
        if account is None:
            return None
        tone = (account.writing_tone or "").strip()
        hook = (account.hook_style or "").strip()
    lines = []
    if tone:
        lines.append(f"Tone: {tone}")
    if hook:
        lines.append(f"Hook style: {hook}")
    return "\n".join(lines) or None


def _with_local_transcript(evidence: title_lab.ClipEvidence) -> title_lab.ClipEvidence:
    """Transcribe the clip with faster-whisper, in memory.

    The result is deliberately NOT written back to ``transcript_text``: this is a
    bench, and a run that quietly edits library rows is a run you cannot repeat
    cleanly. Persisting a transcript is what the app's own Processing job is for.

    A clip with no speech is the normal case here, not an error - most reposted
    reels are music over footage - so it degrades to no transcript and says so.
    """
    from nicheflow_studio.processing.transcription import generate_transcript_draft

    source = Path(evidence.video_path or "")
    if not source.exists():
        print("  no video on disk; skipping transcription")
        return evidence
    print("  transcribing locally (faster-whisper, first run downloads the model)...")
    try:
        draft = generate_transcript_draft(source)
    except RuntimeError as exc:
        print(f"  no transcript: {exc}")
        return evidence
    text = draft.transcript_text.strip()
    print(f"  transcript: {len(text.split())} words")
    return replace(evidence, transcript=text)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_prompts(args: argparse.Namespace) -> int:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = data_dir() / "title_lab" / f"run-{stamp}-item{args.item_id}"
    replies_dir = run_dir / "replies"
    replies_dir.mkdir(parents=True, exist_ok=True)

    evidence = gather_evidence(args.item_id, sheet_target=run_dir / "contact_sheet.jpg")
    if args.transcribe and not (evidence.transcript or "").strip():
        evidence = _with_local_transcript(evidence)
    (run_dir / "evidence.json").write_text(
        json.dumps(evidence.__dict__, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    for variant in args.only or title_lab.VARIANTS:
        prompt = title_lab.build_prompt(
            evidence,
            variant=variant,
            count=args.count,
            seed=args.seed,
        )
        (run_dir / f"{variant}.txt").write_text(prompt, encoding="utf-8")
        placeholder = replies_dir / f"{variant}.txt"
        if not placeholder.exists():
            placeholder.write_text("", encoding="utf-8")

    print(f"Run folder: {run_dir}")
    print()
    for variant in args.only or title_lab.VARIANTS:
        print(f"  {title_lab.VARIANT_LABELS[variant]}")
        print(f"    prompt: {run_dir / f'{variant}.txt'}")
        print(f"    reply:  {replies_dir / f'{variant}.txt'}")
    print()
    if evidence.contact_sheet:
        print(f"Attach this image with every prompt: {evidence.contact_sheet}")
    elif evidence.vision_json:
        print("Vision JSON is embedded in the prompts; no image attachment needed.")
    else:
        print(
            "No vision payload and no contact sheet: the prompts are text-only, which "
            "weakens all three variants equally but makes the run less informative."
        )
    print()
    print("Paste each prompt into the SAME model in a FRESH chat, save each reply into")
    print("the matching replies file, then run:")
    print(f"  .venv\\Scripts\\python.exe scripts\\title_lab.py score --run {run_dir}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Render one batch prompt per variant over several clips.

    Transcription defaults OFF here, unlike the single-clip path. Production
    batches carry no transcripts today (0 of 1,858 library items have one), so a
    transcribed bench batch would measure a pipeline that does not exist yet.
    Pass --transcribe to see what the richer version would look like.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = data_dir() / "title_lab" / f"batch-{stamp}"
    replies_dir = run_dir / "replies"
    replies_dir.mkdir(parents=True, exist_ok=True)

    evidences = []
    for index, item_id in enumerate(args.item_ids, start=1):
        print(f"clip {index} (item {item_id}):")
        evidence = gather_evidence(
            item_id, sheet_target=run_dir / f"clip_{index}_item{item_id}.jpg"
        )
        if args.transcribe and not (evidence.transcript or "").strip():
            evidence = _with_local_transcript(evidence)
        evidences.append(evidence)

    (run_dir / "evidence.json").write_text(
        json.dumps([e.__dict__ for e in evidences], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # The source account's real title for each clip, pulled out of the evidence so
    # it can be read next to the generated lines. Coverage is thin (OCR reaches
    # about 14% of scraped candidates), so clips without one are listed as absent
    # rather than skipped - a gap in the comparison should be visible.
    originals = [
        f"- **Clip {i}** (item {e.item_id}): {e.on_screen_hook or '_(no OCR capture)_'}"
        for i, e in enumerate(evidences, start=1)
    ]
    (run_dir / "originals.md").write_text(
        "\n".join(["# What the source account actually used", "", *originals, ""]),
        encoding="utf-8",
    )

    variants = args.only or [v for v in title_lab.VARIANTS if v != title_lab.VARIANT_CURRENT]
    for variant in variants:
        prompt = title_lab.build_batch_prompt(
            evidences, variant=variant, count=args.count, seed=args.seed
        )
        (run_dir / f"{variant}.txt").write_text(prompt, encoding="utf-8")
        placeholder = replies_dir / f"{variant}.txt"
        if not placeholder.exists():
            placeholder.write_text("", encoding="utf-8")

    sheets = [e.contact_sheet for e in evidences if e.contact_sheet]
    print()
    print(f"Run folder: {run_dir}")
    print(f"{len(evidences)} clips, {len(sheets)} contact sheets to attach")
    print()
    for variant in variants:
        print(f"  {title_lab.VARIANT_LABELS[variant]}")
        print(f"    prompt: {run_dir / f'{variant}.txt'}")
        print(f"    reply:  {replies_dir / f'{variant}.txt'}")
    print()
    print("Attach ALL contact sheets to each chat, in clip order:")
    for sheet in sheets:
        print(f"  {sheet}")
    print()
    print(f"  .venv\\Scripts\\python.exe scripts\\title_lab.py score --run {run_dir}")
    return 0


def cmd_prod(args: argparse.Namespace) -> int:
    """Render the production-shaped prompt: one title per reel, importable.

    The reel delimiters come from ``draft_handoff._batch_item_delimiter`` and the
    account-relative sequence numbers from ``library.account_sequence_map`` -
    the same two sources the shipping prompt uses - so the headers this emits are
    byte-identical to the ones the importer expects to route on.
    """
    from nicheflow_studio.processing import smart_drafts
    from nicheflow_studio.services import draft_generation, draft_handoff, library

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = data_dir() / "title_lab" / f"prod-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Same walk order the production batch uses, so reel N here and reel N there
    # are the same clip.
    item_ids = list(args.item_ids or [])
    if args.account:
        with get_session() as session:
            match = [
                a for a in session.query(Account).all()
                if args.account.lower() in (a.name or "").lower()
            ]
        if len(match) != 1:
            names = ", ".join(a.name for a in match) or "nothing"
            raise SystemExit(f"--account {args.account!r} matched {names}; be more specific.")
        groups = draft_handoff.batch_candidates(
            account_ids=[match[0].id], per_account=args.limit
        )
        item_ids = [int(row["id"]) for group in groups for row in group["items"]]
        available = sum(int(g["available"]) for g in groups)
        print(f"{match[0].name}: {len(item_ids)} of {available} draftless reels")
    if not item_ids:
        raise SystemExit("No reels to draft. Pass --item-ids or --account.")
    ids = draft_handoff.multi_account_batch_order(item_ids)
    with get_session() as session:
        seq_by_item = library.account_sequence_map(session)
        accounts = [
            session.get(Account, session.get(DownloadItem, item_id).account_id)
            for item_id in ids
        ]
        names = sorted({a.name for a in accounts if a})
        # The account "voice" is deliberately NOT passed to the caption rules.
        # effective_caption_rules reads exactly one field from it,
        # target_audience, and uses it only to name the exact string in a
        # don't-paste-the-audience-field ban; the generic wording of that ban
        # survives without it. Meanwhile the account fields that carry actual
        # register - writing_tone ("documentary feel, factual, quietly
        # dramatic") and hook_style ("full-sentence context that sets scene and
        # stakes") - are the voice this whole rewrite exists to get away from,
        # and were never wired in here. Dropping the audience slice makes the
        # rule block identical for every account, which is what lets one prompt
        # cover a mixed batch. Per-clip Account and Niche lines still go in the
        # evidence, so the model still knows whose reel it is writing for.
        caption_rules = smart_drafts.effective_caption_rules(None)
        # The follow outro IS genuinely per-account, so a mixed batch is only
        # refused when the accounts actually resolve different ones. Today
        # _CAPTION_OUTRO_ENABLED is False and every account returns None, so
        # this never fires - it is here for when that is switched back on.
        outros = {draft_generation.caption_outro_for_account(a) for a in accounts if a}
        outros.discard(None)
        if len(outros) > 1:
            raise SystemExit(
                f"These accounts ({', '.join(names)}) resolve different caption outros, "
                "and this prompt states the caption rules once for the whole batch. "
                "Split the batch by account."
            )
        caption_outro = outros.pop() if outros else None

    evidences = []
    delimiters = []
    for index, item_id in enumerate(ids, start=1):
        print(f"reel {index} (item {item_id}):")
        evidence = gather_evidence(
            item_id, sheet_target=run_dir / f"reel_{index}_item{item_id}.jpg"
        )
        if args.transcribe and not (evidence.transcript or "").strip():
            evidence = _with_local_transcript(evidence)
        evidences.append(evidence)
        # The GLOBAL item id, matching import_multi_account_batch_draft's
        # identity routing. Using the account-relative seq here built headers the
        # importer could not resolve, so every reply fell back to routing by reel
        # POSITION - correct only while nothing reorders.
        delimiters.append(
            draft_handoff._batch_item_delimiter(index, item_id, evidence.account_name)
        )

    prompt = title_lab.build_production_batch_prompt(
        evidences,
        variant=args.variant,
        delimiters=delimiters,
        caption_rules=caption_rules,
        caption_outro=caption_outro,
        option_count=args.options,
        seed=args.seed,
    )
    # Pinned here, not held in UI state. Three runs can be in flight at once and
    # each import reads its own targets - the app's Batch Drafts screen keeps a
    # single `preparedIds`, so preparing a second account there overwrites the
    # first and the reply misroutes by reel position.
    (run_dir / "targets.json").write_text(json.dumps(ids), encoding="utf-8")
    (run_dir / "replies").mkdir(exist_ok=True)
    prompt_path = run_dir / f"{args.variant}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    written = [prompt_path]

    if args.with_account_voice:
        # Built from the SAME gathered evidence, not a second run. Transcription
        # is non-deterministic: two --transcribe runs over one clip produced
        # "I was kidding! I was a fine boy!" and "I was kidding, I was a boy!",
        # which would have moved a second variable and made the comparison
        # unreadable.
        voiced = [
            replace(e, account_style=_account_style_lines(e.item_id)) for e in evidences
        ]
        voiced_prompt = title_lab.build_production_batch_prompt(
            voiced,
            variant=args.variant,
            delimiters=delimiters,
            caption_rules=caption_rules,
            caption_outro=caption_outro,
            seed=args.seed,
        )
        voiced_path = run_dir / f"{args.variant}-with-account-voice.txt"
        voiced_path.write_text(voiced_prompt, encoding="utf-8")
        written.append(voiced_path)

    print()
    print(f"Run folder: {run_dir}")
    for path in written:
        print(f"Prompt:     {path}")
    print(f"Variant:    {title_lab.VARIANT_LABELS[args.variant]}")
    print()
    print("Reel headers (the importer routes on these):")
    for delimiter in delimiters:
        print(f"  {delimiter}")
    sheets = [e.contact_sheet for e in evidences if e.contact_sheet]
    if sheets:
        print()
        print("Attach in reel order:")
        for sheet in sheets:
            print(f"  {sheet}")
    print()
    print(f"Accounts:   {', '.join(names)}")
    print("Save the reply into the replies folder above, then import it with:")
    print(f"  .venv\Scripts\python.exe scripts\title_lab.py import --run {run_dir}")
    print("Runs are independent: prepare all three accounts first, then import each")
    print("reply whenever it comes back.")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Import one run's reply against that run's OWN pinned targets.

    This is what makes several accounts safe to have in flight at once. The
    app's Batch Drafts screen keeps a single ``preparedIds``, so preparing a
    second account overwrites the first; a reply that arrives afterwards routes
    by reel POSITION onto the wrong account's clips, silently, because the
    echoed ``(#id)`` is not in the second account's id map. Here the targets
    live in the run folder next to the prompt they were built from, so a reply
    can only ever land on the reels it was written for.

    Writes draft revisions to the database - the same call the app's step 2
    makes, with the same guard rails.
    """
    from nicheflow_studio.services import draft_handoff

    run_dir = Path(args.run)
    targets_path = run_dir / "targets.json"
    if not targets_path.exists():
        raise SystemExit(f"{run_dir} has no targets.json; it was not made by `prod`.")
    targets = json.loads(targets_path.read_text(encoding="utf-8"))

    reply_path = Path(args.file) if args.file else run_dir / "replies" / f"{args.variant}.txt"
    if not reply_path.exists() or not reply_path.read_text(encoding="utf-8").strip():
        raise SystemExit(f"No reply saved at {reply_path}. Paste the model's reply there first.")
    text = reply_path.read_text(encoding="utf-8")

    print(f"Run     : {run_dir.name}")
    print(f"Targets : {targets}")
    result = draft_handoff.import_multi_account_batch_draft(text, targets)
    print()
    print(f"imported : {len(result['imported'])} -> {result['imported']}")
    if result["failed"]:
        print(f"FAILED   : {len(result['failed'])}")
        for row in result["failed"]:
            print(f"   item {row['item_id']}: {row['error']}")
    if result["skipped"]:
        print(f"skipped  : {[r['item_id'] for r in result['skipped']]} (rejected since prepare)")
    if result["unmatched"]:
        print(f"UNMATCHED: {result['unmatched']} - no block in the reply routed to these")
    print()
    print("Open the app's Batch Drafts screen and use '3. Finish batch' to apply them.")
    return 0 if not result["failed"] else 1


def cmd_score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    replies_dir = run_dir / "replies"
    if not replies_dir.is_dir():
        raise SystemExit(f"No replies folder under {run_dir}.")

    wanted = args.only or list(title_lab.VARIANTS)
    # (variant, title, clip index) - clip is 0 for a single-clip run, which sorts
    # and groups the same way without a separate code path.
    pool: list[tuple[str, str, int]] = []
    for variant in wanted:
        reply_path = replies_dir / f"{variant}.txt"
        text = reply_path.read_text(encoding="utf-8") if reply_path.exists() else ""
        # A batch reply carries CLIP delimiters; a single-clip reply does not.
        # Detected rather than flagged, so a batch scored without --batch cannot
        # silently collapse eight clips into one undifferentiated pile.
        blocks = title_lab.parse_batch_titles(text)
        if blocks:
            total = sum(len(v) for v in blocks.values())
            print(f"{variant}: {total} lines across {len(blocks)} clips {sorted(blocks)}")
            pool.extend((variant, title, clip) for clip, ts in blocks.items() for title in ts)
        else:
            titles = title_lab.parse_titles(text)
            print(f"{variant}: {len(titles)} candidate lines")
            pool.extend((variant, title, 0) for title in titles)

    if not pool:
        raise SystemExit("No candidate lines parsed. Are the replies saved?")

    counts = {variant: sum(1 for v, _, _ in pool if v == variant) for variant in wanted}
    if len(set(counts.values())) > 1:
        print()
        print(f"  uneven counts {counts}: scores below are averaged, not summed,")
        print("  but a variant with far fewer lines is still a weaker comparison.")

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    random.Random(seed).shuffle(pool)
    # Shuffle first, then group: lines stay blind within a clip, but a batch is
    # judged clip by clip. Comparing clip 3's line against clip 7's is comparing
    # two different videos, which is not a judgement about the prompt.
    pool.sort(key=lambda row: row[2])

    key = {str(index): variant for index, (variant, _, _) in enumerate(pool, start=1)}
    (run_dir / "key.json").write_text(
        json.dumps({"seed": seed, "lines": key}, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# Blind scorecard",
        "",
        "Tick what applies. Do not look at key.json until you are done.",
        "",
        "Then rank. The boxes stop discriminating once every line clears the bar,",
        "which is the normal outcome of a good prompt: forcing a choice is what",
        "separates two variants that both passed. Write line numbers, best first.",
        "",
        "**Top 3:**",
        "**Worst:**",
        "",
    ]
    seen_clips: set[int] = set()
    flagged = 0
    for index, (_, title, clip) in enumerate(pool, start=1):
        if clip and clip not in seen_clips:
            seen_clips.add(clip)
            lines += [f"## Clip {clip}", ""]
        # Shown rather than filtered: a line that will not render is still worth
        # judging on voice, and hiding it would quietly shrink one variant's pool.
        reason = title_lab.check_length(title)
        if reason:
            flagged += 1
        lines += [
            f"### {index}",
            f"> {title}",
            "",
            *([f"`DOES NOT FIT - {reason}`", ""] if reason else []),
            *(f"- [ ] {criterion}" for criterion in CRITERIA),
            *(f"- [ ] {criterion}" for criterion in FAIL_CRITERIA),
            "",
        ]
    scorecard = run_dir / "scorecard.md"
    scorecard.write_text("\n".join(lines), encoding="utf-8")

    print()
    if flagged:
        print(f"{flagged} of {len(pool)} lines will not render as written (marked in the scorecard)")
    else:
        print(f"all {len(pool)} lines fit the overlay")
    print(f"Scorecard: {scorecard}")
    print(f"Key:       {run_dir / 'key.json'}  (do not open it yet)")
    print()
    print("Fill the scorecard, then run:")
    print(f"  .venv\\Scripts\\python.exe scripts\\title_lab.py reveal --run {run_dir}")
    return 0


_SECTION_RE = re.compile(r"^###\s+(\d+)\s*$")
_RANK_RE = re.compile(r"^\*\*(Top 3|Worst):\*\*\s*(.+?)\s*$", re.IGNORECASE)
_CHECKED_RE = re.compile(r"^-\s*\[[xX]\]\s*(.+?)\s*$")


def _read_rankings(path: Path) -> dict[str, list[str]]:
    """The filled-in Top 3 / Worst lines, as line numbers in the order written."""
    rankings: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = _RANK_RE.match(raw)
        if match:
            picks = [chunk.strip() for chunk in re.split(r"[,\s]+", match.group(2)) if chunk.strip()]
            rankings[match.group(1).title()] = [p for p in picks if p.isdigit()]
    return rankings


def _read_scorecard(path: Path) -> dict[str, set[str]]:
    checked: dict[str, set[str]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        section = _SECTION_RE.match(raw)
        if section:
            current = section.group(1)
            checked.setdefault(current, set())
            continue
        mark = _CHECKED_RE.match(raw)
        if mark and current is not None:
            checked[current].add(mark.group(1))
    return checked


def cmd_reveal(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    key_path = run_dir / "key.json"
    scorecard = run_dir / "scorecard.md"
    if not key_path.exists() or not scorecard.exists():
        raise SystemExit(f"Run {run_dir} has no key.json / scorecard.md; run score first.")

    key = json.loads(key_path.read_text(encoding="utf-8"))["lines"]
    checked = _read_scorecard(scorecard)

    tally: dict[str, dict[str, int]] = {
        variant: {"lines": 0, "points": 0, "fails": 0} for variant in title_lab.VARIANTS
    }
    for index, variant in key.items():
        marks = checked.get(index, set())
        bucket = tally[variant]
        bucket["lines"] += 1
        bucket["points"] += sum(1 for criterion in CRITERIA if criterion in marks)
        bucket["fails"] += sum(1 for criterion in FAIL_CRITERIA if criterion in marks)

    print(f"Run: {run_dir}")
    print()
    for variant in title_lab.VARIANTS:
        bucket = tally[variant]
        if not bucket["lines"]:
            continue
        mean = bucket["points"] / (bucket["lines"] * len(CRITERIA))
        print(f"{title_lab.VARIANT_LABELS[variant]}")
        print(
            f"  {bucket['lines']} lines, {mean:.0%} of criteria met, "
            f"{bucket['fails']} hard fail(s)"
        )
    rankings = _read_rankings(scorecard)
    for label in ("Top 3", "Worst"):
        picks = rankings.get(label) or []
        if picks:
            print(f"{label}: " + ", ".join(f"{n} ({key.get(n, '?')})" for n in picks))
    if rankings:
        print()

    print("Line by line:")
    for index in sorted(key, key=int):
        marks = checked.get(index, set())
        score = sum(1 for criterion in CRITERIA if criterion in marks)
        flag = "  FAIL" if any(criterion in marks for criterion in FAIL_CRITERIA) else ""
        print(f"  {index:>3}  {key[index]}  {score}/{len(CRITERIA)}{flag}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prompts = sub.add_parser("prompts", help="render the three variant prompts for one clip")
    prompts.add_argument("--item-id", type=int, required=True)
    prompts.add_argument("--count", type=int, default=title_lab.DEFAULT_CANDIDATE_COUNT)
    prompts.add_argument("--seed", type=int, default=None, help="fix the example rotation")
    prompts.add_argument(
        "--transcribe",
        action="store_true",
        help="run faster-whisper over the clip when it has no stored transcript (in memory only)",
    )
    prompts.add_argument(
        "--only",
        nargs="+",
        choices=title_lab.VARIANTS,
        help="render just these variants, for when one is already settled",
    )
    prompts.set_defaults(func=cmd_prompts)

    batch = sub.add_parser("batch", help="render one batch prompt covering several clips")
    batch.add_argument("--item-ids", type=int, nargs="+", required=True)
    batch.add_argument("--count", type=int, default=title_lab.DEFAULT_CANDIDATE_COUNT)
    batch.add_argument("--seed", type=int, default=None)
    batch.add_argument("--transcribe", action="store_true")
    batch.add_argument(
        "--only",
        nargs="+",
        choices=[v for v in title_lab.VARIANTS if v != title_lab.VARIANT_CURRENT],
        help="render just these variants (the control does not batch)",
    )
    batch.set_defaults(func=cmd_batch)

    prod = sub.add_parser("prod", help="titles per reel, in the format the app imports")
    prod.add_argument(
        "--options",
        type=int,
        default=3,
        help="titles per reel; the app's option cards expect 3 and pad blanks below that",
    )
    prod.add_argument("--item-ids", type=int, nargs="+")
    prod.add_argument("--account", help="draft this account's draftless reels (name or part of it)")
    prod.add_argument("--limit", type=int, default=6, help="max reels when using --account")
    prod.add_argument("--variant", default=title_lab.VARIANT_V2_NAMED,
                      choices=[v for v in title_lab.VARIANTS if v != title_lab.VARIANT_CURRENT])
    prod.add_argument("--seed", type=int, default=None)
    prod.add_argument("--transcribe", action="store_true")
    prod.add_argument(
        "--with-account-voice",
        action="store_true",
        help="inject the account's writing_tone/hook_style the way production does",
    )
    prod.set_defaults(func=cmd_prod)

    imp = sub.add_parser("import", help="import a run's reply against its own pinned targets")
    imp.add_argument("--run", required=True)
    imp.add_argument("--variant", default=title_lab.VARIANT_V2_NAMED)
    imp.add_argument("--file", help="reply file, if not replies/<variant>.txt")
    imp.set_defaults(func=cmd_import)

    score = sub.add_parser("score", help="shuffle the replies into a blind scorecard")
    score.add_argument("--run", required=True)
    score.add_argument("--seed", type=int, default=None, help="fix the shuffle")
    score.add_argument(
        "--only",
        nargs="+",
        choices=title_lab.VARIANTS,
        help="score just these variants, for when one is already settled",
    )
    score.set_defaults(func=cmd_score)

    reveal = sub.add_parser("reveal", help="map the filled scorecard back to variants")
    reveal.add_argument("--run", required=True)
    reveal.set_defaults(func=cmd_reveal)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
