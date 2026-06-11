"""Manual chat and coding-agent handoff for Processing drafts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import smart_drafts
from nicheflow_studio.services import draft_revisions, publishing_dashboard
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
        match_result = next(
            ((kind, match) for kind, pattern in _HEADERS if (match := pattern.match(raw_line.strip()))),
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
        if not note:
            continue
        match = _NOTE_LINE.match(note)
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


def build_chat_prompt(item_id: int, settings: dict | None = None) -> str:
    settings = settings or {}
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        account = session.get(Account, item.account_id) if item.account_id else None
        path = Path(item.file_path or "").expanduser().resolve()
        niche_label = account.niche_label if account and account.niche_label else None
        few_shot_winners = (
            publishing_dashboard.top_post_titles(account.id) if account is not None else []
        )
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
    )
    hook_rules = smart_drafts._hook_drama_and_fact_safety_rules()
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
            "On-screen title rules (follow these exactly):",
            *title_rules,
            "",
            "Hook framing (drama is allowed, overclaiming is not):",
            *hook_rules,
            "",
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
            "strings, one per option, NOT an object), recommended_title_index, "
            "recommended_caption_index, recommendation_reason, provider_label, and source.",
            "- Recommended indexes are 1-based and MUST be equal to each other. Set "
            "provider_label to 'Codex' or 'Claude Code' and source to 'codex' or "
            "'claude-code' (a short label, never a file path).",
            "- The running Processing screen automatically detects the saved revision. Do not ask the user to paste it manually.",
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


def import_pasted_draft(item_id: int, text: str) -> DraftRevisionDTO:
    parsed = parse_pasted_draft(text)
    if not any(value.strip() for value in [*parsed.title_options, *parsed.caption_options]):
        raise DraftRevisionError(
            "No 'Title Option' or 'Caption Option' sections found in the clipboard text."
        )
    return draft_revisions.save_revision(
        item_id,
        title_options=parsed.title_options,
        caption_options=parsed.caption_options,
        option_notes=parsed.option_notes,
        recommended_title_index=parsed.recommended_title_index,
        recommended_caption_index=parsed.recommended_caption_index,
        recommendation_reason=parsed.reason,
        source="clipboard",
    )
