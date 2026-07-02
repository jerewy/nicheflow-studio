"""Manual chat and coding-agent handoff for Processing drafts."""

from __future__ import annotations

import datetime as dt
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.paths import processed_dir
from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.post_metrics import top_titles_for_account
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import draft_guard, smart_drafts
from nicheflow_studio.services import draft_generation, draft_revisions, export as export_svc
from nicheflow_studio.services.draft_revisions import DraftRevisionDTO, DraftRevisionError


@dataclass(frozen=True)
class PastedDraft:
    title_options: list[str]
    caption_options: list[str]
    option_notes: list[str]
    recommended_title_index: int | None
    recommended_caption_index: int | None
    reason: str | None


_HEADERS: list[tuple[str, re.Pattern[str]]] = [
    ("title", re.compile(r"^title\s*option\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)),
    ("caption", re.compile(r"^caption\s*option\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)),
    ("style", re.compile(r"^recommended\s*style\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)),
    ("pick", re.compile(r"^recommended\s*pick\s*:\s*(.*)$", re.IGNORECASE)),
    ("why", re.compile(r"^why\s*:\s*(.*)$", re.IGNORECASE)),
    ("notes", re.compile(r"^selection\s*notes\s*:\s*(.*)$", re.IGNORECASE)),
]
_NOTE_LINE = re.compile(r"^option\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)
# Chat models often append a sources block after the last reel: a markdown
# reference-style footnote ("[1]: https://...") or a bare URL line. These are
# not selection notes, and because they trail the final block they otherwise
# bleed into that reel's last note. Drop them when building notes.
_CITATION_LINE = re.compile(r"^\[\d+\]\s*:|^https?://\S+$", re.IGNORECASE)
# A reel header line in a pasted batch reply. Primary routing is by the REEL
# NUMBER (group 1), which models reproduce reliably. The prompt also asks the
# model to echo the account-relative "(#id)" (group 2); when it survives, import
# prefers it because it pins the block to a specific video regardless of order.
# An absent/garbled id just leaves group 2 None and falls back to the reel number
# — the id never misroutes on its own. Tolerant of: leading/trailing "=", markdown
# (** , ## ), an optional "#", and a trailing "| item N | label" the model may or
# may not echo. A normal prose line like "Reel 1 of the series" is rejected
# because the char after the number must be a delimiter (:|)-=#*_ ), not a letter.
_REEL_HEADER = re.compile(
    r"^\s*(?:[*_#=]+\s*)?reel\s*#?\s*(\d+)"
    r"(?:\s*\(\s*#?\s*(\d+)\s*\))?"
    r"\s*(?:[:|)\-=#*_].*)?$",
    re.IGNORECASE,
)


def _strip_header_emphasis(line: str) -> str:
    """Unwrap markdown the chat model wraps a SECTION HEADER in.

    Chat models keep bolding headers as ``**Title Option 1:**`` despite the
    plain-text instruction, and every header regex below is anchored at ``^`` --
    so a leading ``**`` makes the line fail to match and the whole reply imports
    as zero sections (the "No 'Title Option' ... found" failure the user hit).

    We unwrap only the label side (before the first colon) plus the closing
    emphasis that lands right after the colon, and only when the label was
    actually emphasized, so ``**bold**`` inside a title VALUE is left intact
    (``join_title`` strips a fully wrapped value separately). The result is used
    ONLY for header detection; the raw line is still what gets buffered, so a
    caption body that merely contains a colon is never rewritten.
    """
    head, sep, rest = line.strip().partition(":")
    if not sep:
        return line
    head_emphasized = "*" in head or "_" in head or head.lstrip().startswith(("#", ">"))
    if not head_emphasized:
        return line
    cleaned_head = re.sub(r"[*_#>`]+", "", head).strip()
    cleaned_rest = re.sub(r"^\s*[*_]{1,3}", "", rest)
    return f"{cleaned_head}:{cleaned_rest}"


def parse_pasted_draft(text: str) -> PastedDraft:
    indexed: dict[str, dict[int, list[str]]] = {"title": {}, "caption": {}, "style": {}}
    singleton: dict[str, list[str]] = {"pick": [], "why": [], "notes": []}
    active_kind: str | None = None
    active_index: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if active_kind in indexed and active_index is not None:
            indexed[active_kind].setdefault(active_index, []).extend(buffer)
        elif active_kind in singleton:
            singleton[active_kind].extend(buffer)
        buffer = []

    for raw_line in (text or "").splitlines():
        header_line = _strip_header_emphasis(raw_line.strip())
        match_result = next(
            ((kind, match) for kind, pattern in _HEADERS if (match := pattern.match(header_line))),
            None,
        )
        if match_result is None:
            buffer.append(raw_line.rstrip())
            continue
        flush()
        active_kind, match = match_result
        if active_kind in indexed:
            active_index = int(match.group(1))
            inline = match.group(2).strip()
            # A repeated "Title/Caption/Style Option N:" header redefines that
            # slot rather than appending to it. Chat models (and hand-edits)
            # sometimes restate an option -- e.g. a leading summary list of all
            # three titles followed by the same titles interleaved with their
            # captions -- and flush()'s extend() would otherwise concatenate the
            # text into itself ("Janet... Michael Janet... Michael"). Clear the
            # slot so the latest declaration wins; continuation lines are not
            # headers and never reach here, so multi-paragraph captions are safe.
            indexed[active_kind].pop(active_index, None)
        else:
            active_index = None
            inline = match.group(1).strip()
        buffer = [inline] if inline else []
    flush()

    def join_line(lines: list[str]) -> str:
        return " ".join(part.strip() for part in lines if part.strip()).strip()

    def join_title(lines: list[str]) -> str:
        title = join_line(lines)
        if title.startswith("**") and title.endswith("**") and title.count("**") == 2:
            return title[2:-2].strip()
        return title

    def join_caption(lines: list[str]) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())

    max_index = max(
        [*indexed["title"], *indexed["caption"], *indexed["style"]],
        default=0,
    )
    titles = [join_title(indexed["title"].get(i, [])) for i in range(1, max_index + 1)]
    captions = [join_caption(indexed["caption"].get(i, [])) for i in range(1, max_index + 1)]
    styles = [join_line(indexed["style"].get(i, [])) for i in range(1, max_index + 1)]
    while titles and not titles[-1] and not captions[-1]:
        titles.pop()
        captions.pop()
        styles.pop()

    note_map: dict[int, list[str]] = {}
    last_note_index: int | None = None
    for line in singleton["notes"]:
        note = line.strip()
        if not note or _CITATION_LINE.match(note):
            continue
        match = _NOTE_LINE.match(_strip_header_emphasis(note))
        if match:
            last_note_index = int(match.group(1))
            note_map.setdefault(last_note_index, []).append(match.group(2).strip())
        elif last_note_index is not None:
            note_map[last_note_index].append(note)
    notes = []
    note_count = max([len(titles), *note_map.keys()], default=0)
    for index in range(1, note_count + 1):
        note = join_line(note_map.get(index, []))
        style = styles[index - 1] if index <= len(styles) else ""
        notes.append(f"Style: {style}. {note}".strip() if style else note)

    pick = " ".join(singleton["pick"])
    title_pick = re.search(r"title\s*option\s*(\d+)", pick, re.IGNORECASE)
    caption_pick = re.search(r"caption\s*option\s*(\d+)", pick, re.IGNORECASE)
    return PastedDraft(
        title_options=titles,
        caption_options=captions,
        option_notes=notes,
        recommended_title_index=int(title_pick.group(1)) if title_pick else None,
        recommended_caption_index=int(caption_pick.group(1)) if caption_pick else None,
        reason=join_line(singleton["why"]) or None,
    )


def _caption_rule_voice(account: Account | None) -> dict[str, str] | None:
    """The minimal account-voice slice the caption rules need.

    ``effective_caption_rules`` only reads ``target_audience`` (to ban the
    audience-label leak); pass it through so the chat path bans the exact
    string, matching the live generation prompt. Returns None when there is no
    audience set, which makes the rule fall back to its generic wording.
    """
    if account is None:
        return None
    audience = (account.target_audience or "").strip()
    return {"target_audience": audience} if audience else None


# Title Option and Caption Option are different texts, but the historytrails
# title rules describe the on-screen title itself as "the long, calm documentary
# caption that sits at the top of the clip". With a separate Caption Option field
# in the flat paste format, chat models duplicated the title into the caption
# slot and dumped the real 80-120 word description under an unparsed
# "Caption Option N (full):" header, so the importer only saw the one-line copy.
# Spell the two fields out so the full caption lands in the slot the importer reads.
_FIELD_DISAMBIGUATION_LINES = [
    "Title Option and Caption Option are TWO DIFFERENT texts for each option, "
    "never the same words twice:",
    "- Title Option N is the on-screen overlay line from the on-screen title "
    "rules above (one sentence; its length follows the AUTO MIX bands).",
    "- Caption Option N is the Instagram description from the caption rules "
    "above: about 80-120 words across two paragraphs. Put that full description "
    "directly on and below the 'Caption Option N:' line.",
    "Do NOT copy the title into the caption, do NOT write a one-line caption, and "
    "do NOT add any extra field such as 'Caption Option N (full):'. There is "
    "exactly one 'Caption Option N:' per option and its full caption follows it.",
]

# Explicit verify-and-prune step: tighten green-tier (drop/soften unsupported
# claims, catch source errors) rather than license invented specifics.
_FACT_CHECK_LINES = [
    "Fact-check pass (do this BEFORE writing each reel):",
    "- List the concrete claims you plan to use: names, ages, dates, places, "
    "numbers, causes, and any first/last/only superlative.",
    "- Keep a claim only when THIS reel's source title, source description, "
    "transcript, or visual evidence supports it. If a claim is not supported, "
    "soften it ('decades ago', 'thousands of people') or drop it; never invent a "
    "specific just to reach the title length.",
    "- If a source claim looks wrong or like clickbait ('they never told you', "
    "'darkest secret', or a year or identity you are confident is mistaken), do "
    "not repeat it as fact. Drop the clickbait claim, but keep any substantive, "
    "checkable backstory in the same caption; do not throw out the whole caption. "
    "Use the supported version or leave the bad claim out.",
]


# Reconciles the "story beneath the clip" accounts (e.g. Beneath History) with
# the HistoryTrails "anchor to what's on screen" rules: the source caption is a
# first-class grounding source, so an off-camera backstory the caption supports
# is usable, and a clickbait opener is no reason to discard the real story under
# it. Mirrors the live Groq prompt's PRIMARY-CONTEXT framing on the chat path.
_SOURCE_CAPTION_GUIDANCE = [
    "How to use the source caption (READ THIS BEFORE WRITING):",
    "- PRIMARY CONTEXT: when there is no transcript, the source caption is your main "
    "signal for what this clip is actually about. Identify the specific subject, "
    "people, event, or backstory it names and build the titles and captions directly "
    "around it. Do not retreat to a generic description of the footage when the caption "
    "gives you a real story.",
    "- SEPARATE THE HOOK FROM THE STORY: clickbait-farm captions often open with an "
    "unverifiable teaser ('they never told you', 'secret', 'shocking truth') and then "
    "give a real, substantive backstory underneath. Drop the unverifiable teaser, but "
    "KEEP and tell the substantive story. Still apply the fact-check and green-tier "
    "rules to that story (soften or drop any single specific the caption cannot back), "
    "but never discard the whole caption just because its first line is clickbait.",
    "- THE CLIP IS THE VISUAL, THE CAPTION IS THE STORY: a clip is often only the visual "
    "hook for a story the caption tells, and that story may not be visible in the frame. "
    "The source caption is valid grounding on its own, equal to the visible evidence. A "
    "fact being off-camera is NOT a reason to drop it; only drop a specific the caption "
    "does not actually support. Lead with the story and let the footage be the moment "
    "the viewer watches while you tell it.",
]


def build_chat_prompt(item_id: int, settings: dict | None = None) -> str:
    settings = settings or {}
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        account = session.get(Account, item.account_id) if item.account_id else None
        path = Path(item.file_path or "").expanduser().resolve()
        niche_label = account.niche_label if account and account.niche_label else None
        account_key = account.instagram_handle if account is not None else None
        few_shot_winners = top_titles_for_account(account_key) if account_key else []
        caption_outro = draft_generation.caption_outro_for_account(account)
        caption_voice = _caption_rule_voice(account)
        fields = [
            f"Account: {account.name if account else '(none)'}",
            f"Niche: {niche_label or '(none)'}",
            f"Tone: {account.writing_tone if account and account.writing_tone else '(none)'}",
            f"Target audience: {account.target_audience if account and account.target_audience else '(none)'}",
            f"Hook style: {account.hook_style if account and account.hook_style else '(none)'}",
            f"Banned phrases: {account.banned_phrases if account and account.banned_phrases else '(none)'}",
            f"Title rules: {account.title_style_notes if account and account.title_style_notes else '(none)'}",
            f"Caption rules: {account.caption_style_notes if account and account.caption_style_notes else '(none)'}",
            f"Source title: {item.title or '(none)'}",
            f"Source URL: {item.source_url or '(none)'}",
            f"Source description: {item.source_description or '(none)'}",
        ]
        transcript = (item.transcript_text or "").strip() or "(none; inspect the local video)"
        vision_text = (item.smart_vision_payload or "").strip() or "(none)"
    caption_style = settings.get("caption_style") or None
    title_style = settings.get("title_style") or None
    # Same rule source as the live Groq prompt (see effective_title_rules
    # docstring) so the chat path and the API path never drift apart.
    title_rules = smart_drafts.effective_title_rules(
        title_style,
        caption_style,
        niche_label,
        few_shot_winners=few_shot_winners,
        title_length=settings.get("title_length") or "long",
    )
    hook_rules = smart_drafts._hook_drama_and_fact_safety_rules()
    # Same source as the live CAPTION block so caption length/structure can't
    # drift between the chat path and the API path (titles already share
    # effective_title_rules above; captions used to be a bare style token here).
    caption_rules = smart_drafts.effective_caption_rules(
        caption_style,
        account_voice=caption_voice,
    )
    return "\n".join(
        [
            "Please inspect this local NicheFlow video and generate Instagram-ready drafts.",
            "",
            "Local video path:",
            f'"{path}"',
            "",
            "Account and source context:",
            *fields,
            f"Clip premise / user direction: {settings.get('clip_premise') or '(none)'}",
            f"Caption style: {settings.get('caption_style') or '(none)'}",
            f"Title style: {settings.get('title_style') or 'Auto (match caption style)'}",
            f"Title length: {(settings.get('title_length') or 'long').title()}",
            f"Processing template: {settings.get('template') or '(none)'}",
            "",
            "Visual evidence JSON (from an earlier vision pass; empty means no pass ran):",
            vision_text[:4000],
            "",
            "Existing transcript/context:",
            transcript[:6000],
            "",
            "If you can open local files (Codex, Claude Code): inspect the actual video "
            "frames before writing anything.",
            "If you CANNOT open the local video (chat assistants like ChatGPT or Claude "
            "web): work ONLY from the signals above — source title, source description, "
            "visual evidence JSON, and transcript. If those signals do not clearly "
            "identify the subject of the clip, STOP and ask the user for a one-line "
            "description of what is on screen instead of generating guesses. Never "
            "invent names, dates, places, records, or events the signals do not state.",
            "",
            *_SOURCE_CAPTION_GUIDANCE,
            "",
            "On-screen title rules (follow these exactly):",
            *title_rules,
            "",
            "Hook framing (drama is allowed, overclaiming is not):",
            *hook_rules,
            "",
            "Caption rules (follow these exactly):",
            *caption_rules,
            "",
            *_FACT_CHECK_LINES,
            "",
            *(
                [
                    "",
                    "Caption follow outro (MANDATORY): every caption option must include "
                    f'this exact line, alone on its own line, directly before the hashtag line: "{caption_outro}". '
                    "Do not reword it or merge it into a paragraph.",
                ]
                if caption_outro
                else []
            ),
            "Generate 3 meaningfully different on-screen title options and 3 caption options.",
            "Keep display titles plain text. Only use internal **keyword** emphasis when the Cinema Bold Keywords style requires it.",
            "Never write em dashes or double hyphens ('--') in titles or captions; "
            "use a comma, period, or colon instead. Long dashes read as AI-generated copy.",
            "Recommend the strongest title/caption pair and add one short selection note per option.",
            "The recommended title and caption MUST share the same option number: the app "
            "applies title+caption as ONE unit, so rearrange your options before returning "
            "until the strongest pair sits together (never recommend Title 2 + Caption 3).",
            "Do not invent unsupported facts.",
            "",
            "Automatic NicheFlow handoff for Codex or Claude Code:",
            f"- After generating, save the options directly to item {item_id} in SQLite: "
            "write the draft JSON to a UTF-8 file, then run:",
            f"  .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py save --item-id {item_id} --file <draft-json-path>",
            "- Do NOT pipe the JSON through Get-Content into --stdin: PowerShell re-decodes "
            "the bytes and silently corrupts em dashes and emoji before Python sees them.",
            "- Use JSON fields title_options, caption_options, option_notes (a LIST of "
            "strings, one per option, NOT an object), option_tiers (a LIST of "
            "'green'/'yellow'/'red' strings, one per title, from the HOOK FRAMING tiers "
            "above), recommended_title_index, recommended_caption_index, "
            "recommendation_reason, provider_label, and source.",
            "- Recommended indexes are 1-based and MUST be equal to each other. Set "
            "provider_label to 'Codex' or 'Claude Code' and source to 'codex' or "
            "'claude-code' (a short label, never a file path).",
            "- The running Processing screen automatically detects the saved revision. Do not ask the user to paste it manually.",
            "",
            *_FIELD_DISAMBIGUATION_LINES,
            "",
            "Return format (write every section header exactly as shown, plain text — "
            "never bold or markdown-formatted; '**Title Option 1:**' breaks the importer):",
            "Title Option 1:",
            "Caption Option 1:",
            "Title Option 2:",
            "Caption Option 2:",
            "Title Option 3:",
            "Caption Option 3:",
            "Recommended Pick: Title Option N + Caption Option N",
            "Why:",
            "Selection Notes:",
            "Option 1:",
            "Option 2:",
            "Option 3:",
        ]
    )


def _batch_item_delimiter(index: int, seq: int | None) -> str:
    # Header carries the reel number (which models reproduce reliably) plus the
    # account-relative "#id" the user sees in the list. Import prefers the #id
    # when it survives and falls back to the reel number otherwise, and it
    # validates the #id against this batch — so a dropped or garbled id never
    # misroutes (the failure that retired the older id-in-header scheme).
    if seq is not None:
        return f"===== REEL {index} (#{seq}) ====="
    return f"===== REEL {index} ====="


def _account_prompt_header(
    account: Account | None,
    *,
    settings: dict,
    item_ids: list[int],
) -> list[str]:
    niche_label = account.niche_label if account and account.niche_label else None
    account_key = account.instagram_handle if account is not None else None
    few_shot_winners = top_titles_for_account(account_key) if account_key else []
    title_rules = smart_drafts.effective_title_rules(
        settings.get("title_style") or None,
        settings.get("caption_style") or None,
        niche_label,
        few_shot_winners=few_shot_winners,
        title_length=settings.get("title_length") or "long",
    )
    hook_rules = smart_drafts._hook_drama_and_fact_safety_rules()
    # Same source as the live CAPTION block (see effective_caption_rules); the
    # batch path used to emit only a bare "Caption style:" token, so the web
    # model had no length/structure target and returned one-line captions.
    caption_rules = smart_drafts.effective_caption_rules(
        settings.get("caption_style") or None,
        account_voice=_caption_rule_voice(account),
    )
    return [
        "You are drafting Instagram Reel titles and captions for one NicheFlow account.",
        "This is for ChatGPT or Claude web. The user will attach one still image per reel.",
        "",
        "Shared account voice and style:",
        f"Account: {account.name if account else '(none)'}",
        f"Niche: {niche_label or '(none)'}",
        f"Tone: {account.writing_tone if account and account.writing_tone else '(none)'}",
        f"Target audience: {account.target_audience if account and account.target_audience else '(none)'}",
        f"Hook style: {account.hook_style if account and account.hook_style else '(none)'}",
        f"Banned phrases: {account.banned_phrases if account and account.banned_phrases else '(none)'}",
        f"Title rules: {account.title_style_notes if account and account.title_style_notes else '(none)'}",
        f"Caption rules: {account.caption_style_notes if account and account.caption_style_notes else '(none)'}",
        f"Clip premise / user direction: {settings.get('clip_premise') or '(none)'}",
        f"Caption style: {settings.get('caption_style') or '(none)'}",
        f"Title style: {settings.get('title_style') or 'Auto (match caption style)'}",
        f"Title length: {(settings.get('title_length') or 'long').title()}",
        f"Processing template: {settings.get('template') or '(none)'}",
        f"Number of reels in this batch: {len(item_ids)}",
        "",
        "On-screen title rules (follow these exactly):",
        *title_rules,
        "",
        "Hook framing (drama is allowed, overclaiming is not):",
        *hook_rules,
        "",
        "Caption rules (follow these exactly):",
        *caption_rules,
        "",
        *_FACT_CHECK_LINES,
        "",
        "Plain-text rules:",
        "- Keep display titles plain text.",
        "- Do not use markdown formatting in section headers or titles.",
        "- Never write em dashes or double hyphens ('--') in titles or captions.",
        "- Do not invent unsupported facts.",
        "- The recommended title and caption MUST share the same option number.",
        "",
        "Return one block per reel, one for every reel, in order.",
        "START each reel's block with its own header line that is exactly:",
        "===== REEL <n> (#<id>) =====",
        "copying BOTH the reel number and the (#id) exactly as shown in that reel's "
        "context below (for example '===== REEL 1 (#143) ====='). The (#id) is how the "
        "app files each reply to the correct video, so never change, drop, or invent it.",
        "Do not rename, renumber, merge, or skip any reel, even if two clips look similar.",
        "",
        *_FIELD_DISAMBIGUATION_LINES,
        "Inside each block, use this exact plain-text format:",
        "Title Option 1:",
        "Caption Option 1:",
        "Title Option 2:",
        "Caption Option 2:",
        "Title Option 3:",
        "Caption Option 3:",
        "Recommended Pick: Title Option N + Caption Option N",
        "Why:",
        "Selection Notes:",
        "Option 1:",
        "Option 2:",
        "Option 3:",
    ]


def _batch_item_context(index: int, item: DownloadItem, seq: int | None) -> list[str]:
    transcript = (item.transcript_text or "").strip() or "(none)"
    vision_text = (item.smart_vision_payload or "").strip() or "(none)"
    frame_name = f"reel_{index}_item{item.id}.jpg"
    return [
        _batch_item_delimiter(index, seq),
        f"Attach and inspect image file: {frame_name}",
        f"Source title: {item.title or '(none)'}",
        f"Source URL: {item.source_url or '(none)'}",
        f"Source description: {item.source_description or '(none)'}",
        "",
        "Visual evidence JSON:",
        vision_text[:4000],
        "",
        "Transcript/context excerpt:",
        transcript[:3000],
    ]


def build_account_batch_chat_prompt(
    account_id: int, item_ids: list[int], settings: dict | None = None
) -> str:
    settings = settings or {}
    ids = [int(item_id) for item_id in item_ids]
    if not ids:
        raise DraftRevisionError("Choose at least one item for the batch.")
    with get_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise DraftRevisionError(f"No account with id {account_id}.")
        items = (
            session.query(DownloadItem)
            .filter(DownloadItem.id.in_(ids), DownloadItem.account_id == account_id)
            .all()
        )
        by_id = {item.id: item for item in items}
        missing = [item_id for item_id in ids if item_id not in by_id]
        if missing:
            raise DraftRevisionError(
                f"Item(s) not found for this account: {', '.join(map(str, missing))}."
            )
        ordered = [by_id[item_id] for item_id in ids]
        # Lazy import: keep draft_handoff free of library's downloader import chain.
        from nicheflow_studio.services import library

        seq_by_item = library.account_sequence_map(session)
        lines = _account_prompt_header(account, settings=settings, item_ids=ids)
        lines.extend(["", "Reel contexts:"])
        for index, item in enumerate(ordered, start=1):
            lines.extend(["", *_batch_item_context(index, item, seq_by_item.get(item.id))])
    return "\n".join(lines)


def _split_batch_blocks(text: str) -> dict[int, tuple[int | None, str]]:
    """Split a pasted reply into blocks keyed by 1-based REEL NUMBER, each paired
    with the echoed ``(#id)`` from its header (``None`` when the model dropped it).

    A repeated header for the same reel keeps the first block and its id (later
    headers are treated as continuations of nothing useful and ignored).
    """
    blocks: dict[int, list[str]] = {}
    echoed: dict[int, int | None] = {}
    current_reel: int | None = None
    for line in (text or "").splitlines():
        match = _REEL_HEADER.match(line.strip())
        if match:
            current_reel = int(match.group(1))
            if current_reel not in blocks:
                blocks[current_reel] = []
                echoed[current_reel] = int(match.group(2)) if match.group(2) else None
            continue
        if current_reel is not None:
            blocks[current_reel].append(line)
    return {reel: (echoed[reel], "\n".join(lines).strip()) for reel, lines in blocks.items()}


def _batch_seq_to_item(item_ids: list[int]) -> dict[int, int]:
    """Map {account "#id" -> item_id} for this batch, so a reply that echoes a
    "(#143)" routes to the exact video. Batch items share one account, so their
    per-account sequence numbers are unique within the batch."""
    if not item_ids:
        return {}
    # Lazy import: keep draft_handoff free of library's downloader import chain.
    from nicheflow_studio.services import library

    with get_session() as session:
        seq_by_item = library.account_sequence_map(session)
    return {seq_by_item[item_id]: item_id for item_id in item_ids if item_id in seq_by_item}


def import_account_batch_draft(text: str, item_ids: list[int]) -> dict:
    """Import a batch reply. Each block routes to a specific item by the echoed
    ``(#id)`` when it names an item in this batch (most reliable — survives
    reordering and reel-number drift), otherwise by REEL NUMBER position. An
    ``(#id)`` that is not part of this batch (e.g. the model parroted a frame
    filename's item id) is ignored and the reel number is used instead, so a
    garbled id never misroutes."""
    requested = [int(item_id) for item_id in item_ids]
    seq_to_item = _batch_seq_to_item(requested)
    blocks_by_reel = _split_batch_blocks(text)
    imported: list[int] = []
    failed: list[dict] = []
    seen: set[int] = set()
    for reel, (echoed_id, block) in sorted(blocks_by_reel.items()):
        item_id = seq_to_item.get(echoed_id) if echoed_id is not None else None
        if item_id is None:
            if reel < 1 or reel > len(requested):
                continue  # reel number outside this batch and no valid id — ignore it
            item_id = requested[reel - 1]
        if item_id in seen:
            continue  # duplicate target — first block wins
        seen.add(item_id)
        try:
            import_pasted_draft(item_id, block)
            imported.append(item_id)
        except DraftRevisionError as exc:
            failed.append({"item_id": item_id, "error": str(exc)})
    matched = set(imported) | {row["item_id"] for row in failed}
    return {
        "imported": imported,
        "failed": failed,
        "unmatched": [item_id for item_id in requested if item_id not in matched],
    }


def batch_frames(item_ids: list[int]) -> dict:
    ids = [int(item_id) for item_id in item_ids]
    if not ids:
        raise DraftRevisionError("Choose at least one item for the batch.")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = processed_dir() / "chatgpt-batches" / f"batch-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    for index, item_id in enumerate(ids, start=1):
        source = export_svc.crop_preview_frame(item_id)
        target = folder / f"reel_{index}_item{item_id}.jpg"
        shutil.copyfile(source, target)
        frames.append({"item_id": item_id, "path": str(target)})
    return {"folder": str(folder), "frames": frames}


def import_pasted_draft(item_id: int, text: str) -> DraftRevisionDTO:
    parsed = parse_pasted_draft(text)
    if not any(value.strip() for value in [*parsed.title_options, *parsed.caption_options]):
        raise DraftRevisionError(
            "No 'Title Option' or 'Caption Option' sections found in the clipboard text."
        )
    # Chat assistants can't rate their own output into the paste format, so
    # pasted drafts arrive untiered: the deterministic guard assigns tiers
    # from the stored clip signals and moves the recommended pick off any
    # title whose claim nothing supports. The paste format itself stays
    # untouched — no new headers for a chat model to get wrong.
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id) if item and item.account_id else None
        signals_text = draft_guard.build_signals_text(
            item.transcript_text if item else None,
            item.title if item else None,
            item.source_description if item else None,
            account.niche_label if account else None,
            item.smart_vision_payload if item else None,
        )
    guarded = draft_guard.guard_options(
        title_options=parsed.title_options,
        signals_text=signals_text,
        option_notes=parsed.option_notes,
        recommended_index=(
            parsed.recommended_title_index - 1 if parsed.recommended_title_index else None
        ),
        recommendation_reason=parsed.reason,
    )
    if guarded.recommendation_shifted and guarded.recommended_index is not None:
        recommended_title_index = recommended_caption_index = guarded.recommended_index + 1
    else:
        recommended_title_index = parsed.recommended_title_index
        recommended_caption_index = parsed.recommended_caption_index
    return draft_revisions.save_revision(
        item_id,
        title_options=parsed.title_options,
        caption_options=parsed.caption_options,
        option_notes=guarded.option_notes,
        option_tiers=guarded.option_tiers,
        recommended_title_index=recommended_title_index,
        recommended_caption_index=recommended_caption_index,
        recommendation_reason=guarded.recommendation_reason,
        source="clipboard",
    )
