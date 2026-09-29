"""Manual chat and coding-agent handoff for Processing drafts."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import object_session

from nicheflow_studio.core.paths import processed_dir
from nicheflow_studio.db.models import (
    ScrapeCandidate,
    Account,
    DownloadItem,
    DraftRevision,
    UploadJob,
)
from nicheflow_studio.db.post_metrics import top_titles_for_account
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import (
    draft_guard,
    on_screen_hook,
    smart_drafts,
    title_lab,
    transcription,
    video,
)
from nicheflow_studio.services import draft_generation, draft_revisions, export as export_svc
from nicheflow_studio.services.draft_revisions import DraftRevisionDTO, DraftRevisionError
from nicheflow_studio.services.export import ExportError

logger = logging.getLogger(__name__)


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
    # One caption serving every title option. The Instagram description tells
    # the clip's story while the title is the on-screen hook, so the caption is
    # title-agnostic in practice -- models already collapsed the duplicates
    # themselves ("[Same caption as Option 1]", see _CAPTION_REFERENCE_RE).
    # Asking for it once cuts roughly two thirds of the reply's tokens, which
    # are dominated by the 80-150 word captions.
    ("shared_caption", re.compile(r"^shared\s*caption\s*:\s*(.*)$", re.IGNORECASE)),
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


# Chat models routinely collapse an identical caption to a placeholder like
# "[Same caption as Option 1]" instead of repeating the full text. Left as-is,
# that placeholder becomes the stored caption for Options 2/3 and shows verbatim
# in the review UI and the exported Instagram post. Match a caption whose ENTIRE
# body is only such a reference (case-insensitive, brackets optional, "caption"
# optional) so a caption that merely mentions another option in its prose is
# never rewritten.
_CAPTION_REFERENCE_RE = re.compile(
    r"^\[?\s*same\b[^\]]*?\boption\s*(\d+)\b\s*\]?\s*\.?\s*$",
    re.IGNORECASE,
)


def _resolve_caption_references(captions: list[str]) -> list[str]:
    """Expand placeholder captions ("[Same caption as Option 1]") to the real
    caption they point at, following a reference chain but never looping.

    A broken reference (out of range, self-referential, or pointing at another
    placeholder that can't be resolved) is left untouched, so a malformed
    placeholder degrades to the honest original rather than silently emptying.
    """

    def resolve(index: int, seen: set[int]) -> str:
        caption = captions[index]
        match = _CAPTION_REFERENCE_RE.match(caption.strip())
        if match is None:
            return caption
        target = int(match.group(1)) - 1
        if target == index or target in seen or not (0 <= target < len(captions)):
            return caption
        seen.add(index)
        return resolve(target, seen)

    return [resolve(i, set()) for i in range(len(captions))]


# How many captions the chat model is asked to write per reel.
#   "shared"     -- one 'Shared Caption:' that works under any of the three
#                   titles. Roughly 55% fewer output tokens, and output bills
#                   ~5x input, so this is where the reply's cost actually sits.
#   "per_option" -- a 'Caption Option N:' per title, for reels where the three
#                   title angles genuinely need their own caption.
# This steers the PROMPT only. parse_pasted_draft accepts either shape whatever
# the setting says, so switching modes never strands a reply already in flight.
# The selectable labels live in processing_workflow.CAPTION_MODES; only the raw
# values are needed here (same arrangement as title_length).
# The title style that swaps in the v2 rulebook. Selectable in the Processing
# screen's Title Style dropdown (processing_workflow.TITLE_STYLES) so the two
# rule sets can be A/B'd per account without a code change; every other style
# keeps the historytrails/cinematic blocks it has always used.
TITLE_STYLE_BEATS_V2 = "beats_v2"
# v3 is v2 with ONE wording rule changed: a title may state the fact plainly and
# end on a reaction only that clip could earn, instead of always withholding the
# reveal. Its own style value rather than a flag on v2, so switching one account
# to it is a real A/B and the rest keep the rules they are measured under.
TITLE_STYLE_BEATS_V3 = "beats_v3"

CAPTION_MODE_SHARED = "shared"
CAPTION_MODE_PER_OPTION = "per_option"


def _caption_mode(settings: dict | None) -> str:
    """Normalize the caption-mode setting, defaulting to the cheaper shared caption."""
    value = str((settings or {}).get("caption_mode") or "").strip().lower()
    return CAPTION_MODE_PER_OPTION if value == CAPTION_MODE_PER_OPTION else CAPTION_MODE_SHARED


def _return_format_lines(caption_mode: str) -> list[str]:
    """The exact section headers the model must echo, in the order it writes them.

    Shared mode puts 'Shared Caption:' AFTER the pick and reason so the model
    settles on a winning title before writing the one caption it gets.
    """
    if caption_mode == CAPTION_MODE_PER_OPTION:
        return [
            "Title Option 1:",
            "Caption Option 1:",
            "Title Option 2:",
            "Caption Option 2:",
            "Title Option 3:",
            "Caption Option 3:",
            "Recommended Pick: Title Option N + Caption Option N",
            "Why:",
        ]
    return [
        "Title Option 1:",
        "Title Option 2:",
        "Title Option 3:",
        "Recommended Pick: Title Option N",
        "Why:",
        "Shared Caption:",
    ]


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
    singleton: dict[str, list[str]] = {"pick": [], "why": [], "notes": [], "shared_caption": []}
    active_kind: str | None = None
    active_index: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if active_kind in indexed and active_index is not None:
            # A repeated "Title/Caption/Style Option N:" header redefines that
            # slot rather than appending to it -- models restate an option (a
            # leading summary list of all three titles, then the same titles
            # interleaved with their captions) and appending would concatenate
            # the text into itself ("Janet... Michael Janet... Michael").
            #
            # But an EMPTY restatement must not redefine anything: models also
            # list the three titles up front and then restate a BARE "Title
            # Option 2:" purely as a lead-in to "Caption Option 2:". Wiping the
            # slot there destroyed the real title, so the reply imported with
            # only Option 1 and the screen showed a single card (2026-07-15
            # batch, items 838-843). Keep the earlier declaration in that case.
            if any(line.strip() for line in buffer) or active_index not in indexed[active_kind]:
                indexed[active_kind][active_index] = buffer
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
            # Redefinition is decided in flush(), once the slot's new content is
            # known -- an empty restatement must not clear an earlier value.
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
    captions = _resolve_caption_references(captions)
    styles = [join_line(indexed["style"].get(i, [])) for i in range(1, max_index + 1)]

    # A "Shared Caption:" fills every option that has no caption of its own, so
    # the downstream paired model (title_options[i] pairs with caption_options[i],
    # and Apply applies one option NUMBER as a unit) is unchanged. Per-option
    # captions still win where present, which keeps older 3-caption replies
    # parsing exactly as before.
    shared_caption = join_caption(singleton["shared_caption"])
    if shared_caption:
        if not titles:
            titles = [""]
            styles = [""]
        captions = [
            caption if caption.strip() else shared_caption
            for caption in (captions + [""] * (len(titles) - len(captions)))
        ]

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
    recommended_title_index = int(title_pick.group(1)) if title_pick else None
    recommended_caption_index = int(caption_pick.group(1)) if caption_pick else None
    # With one shared caption there is no "Caption Option N" to name, so the
    # pick line only carries a title. Apply treats the two indexes as one unit,
    # so the caption index has to follow the title's rather than stay None.
    if shared_caption and recommended_caption_index is None:
        recommended_caption_index = recommended_title_index
    return PastedDraft(
        title_options=titles,
        caption_options=captions,
        option_notes=notes,
        recommended_title_index=recommended_title_index,
        recommended_caption_index=recommended_caption_index,
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
# slot and dumped the real full-length description under an unparsed
# "Caption Option N (full):" header, so the importer only saw the one-line copy.
# Spell the two fields out so the full caption lands in the slot the importer reads.
def _field_disambiguation_lines(
    caption_style: str | None, caption_mode: str = CAPTION_MODE_SHARED
) -> list[str]:
    """Spell the two fields apart, echoing the selected style's real length.

    This block used to hardcode "about 80-120 words across two paragraphs"
    (the historytrails_archive shape) for every style. It sits near the end of
    the prompt, so its numbers tend to win over the caption rules above: for
    styles like history_lost_archive (90-150 words, 3 paragraphs + hashtags)
    the stale spec quietly squeezed captions shorter and could drop the payoff
    paragraph. Pull the word target from the same source as the caption rules
    so the two specs can never disagree again, and defer the paragraph shape
    to those rules instead of restating it.
    """
    word_target = smart_drafts._caption_word_target(caption_style)
    if caption_mode == CAPTION_MODE_PER_OPTION:
        return [
            "Title Option and Caption Option are TWO DIFFERENT texts for each option, "
            "never the same words twice:",
            "- Title Option N is the on-screen overlay line from the on-screen title "
            "rules above (one sentence, inside the character ceiling in those rules).",
            f"- Caption Option N is the Instagram description from the caption rules "
            f"above: about {word_target} words, in the exact paragraph structure those "
            "caption rules specify. Writing toward the upper end of that range beats "
            "compressing paragraphs together. Put that full description directly on "
            "and below the 'Caption Option N:' line.",
            "Write all THREE captions in full. Never abbreviate one to a placeholder "
            "like '[Same caption as Option 1]': if the three captions would say the "
            "same thing, that is a sign the three titles are not different enough.",
            "Do NOT copy the title into the caption, do NOT write a one-line caption, and "
            "do NOT add any extra field such as 'Caption Option N (full):'. There is "
            "exactly one 'Caption Option N:' per option and its full caption follows it.",
        ]
    return [
        "Title Option and Shared Caption are TWO DIFFERENT texts, never the same "
        "words twice:",
        "- Title Option N is the on-screen overlay line from the on-screen title "
        "rules above (one sentence, inside the character ceiling in those rules). Write "
        "THREE of these, meaningfully different from each other.",
        f"- Shared Caption is the ONE Instagram description from the caption rules "
        f"above: about {word_target} words, in the exact paragraph structure those "
        "caption rules specify. Writing toward the upper end of that range beats "
        "compressing paragraphs together. Put that full description directly on "
        "and below the 'Shared Caption:' line.",
        "Write the caption ONCE, and write it so it works under ANY of the three "
        "titles: it tells the clip's story, while the title is the on-screen hook. "
        "Do not write it as a continuation of one particular title, and do not "
        "repeat a title's exact wording in its opening line.",
        "Do NOT copy the title into the caption, do NOT write a one-line caption, and "
        "do NOT add any extra field such as 'Shared Caption (full):' or a separate "
        "'Caption Option N:' per title. There is exactly ONE 'Shared Caption:' per reel.",
    ]

# Verify-then-use: the chat path routes through models that can identify
# famous clips and check facts with web search — the one capability Groq lacks.
# The old instruction ("work ONLY from the signals above... STOP and ask")
# forced a capable model to treat a recognizable moment as unverifiable and
# hedge. Measured on the historytrails reference set, name+year titles median
# 87k views and unlock the top formats, so turning research into verified
# specifics is the single biggest engagement lever on this path. Invention
# stays banned: only VERIFIED facts may be used, and their origin must be
# disclosed in the selection note so the reviewer can spot-check.
_WEB_RESEARCH_LINES = [
    "Research (chat assistants with web access):",
    "- If you recognize the person, event, or moment, or can identify it from "
    "the source URL, source caption, or attached frame, VERIFY it with a quick "
    "web search and use the confirmed specifics (names, dates, places) in the "
    "titles and captions. A verified specific beats a hedged vague line.",
    "- State in that option's selection note which facts came from your own "
    "research rather than the provided signals, so the reviewer can spot-check.",
    "- QUOTE RULE for researched specifics (HARD RULE): for every name, date, or "
    "number you add from research, the selection note must give the outlet AND "
    "the shortest verbatim quote from that source stating the fact, e.g. "
    "Washington Post: 'uploaded in April 2015'. Naming an outlet without a "
    "quote does NOT count as verification. If you cannot quote a sentence that "
    "states the fact, it is unverified: soften it ('years later', 'decades "
    "ago') or drop it, exactly like an unsupported source claim.",
    "- Never state a guess you could not verify, and never invent names, dates, "
    "places, records, or events that neither the signals nor verified research "
    "support.",
    "- If the subject stays unidentifiable after research, do NOT hedge and do "
    "NOT describe the recording itself; write from the visible scene per the "
    "thin-evidence rules above, or ask the user for a one-line description if "
    "even the scene is unclear.",
    "- If the source caption CONTRADICTS the footage (it names a period, person, "
    "or event the frames cannot show), drop the caption and write from the "
    "visible scene, noting the mismatch in the selection note. NEVER write that "
    "finding into a Title Option or the caption text: the title is burned onto "
    "the video and the viewer has no access to the source caption, so a line "
    "like 'the caption attached to this clip is wrong' is unpostable, and the "
    "importer rejects a reel whose every title reads that way. When even the "
    "visible scene will not carry a title, write NO options for that reel and "
    "say so in one plain line instead.",
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
    # Same first step as the batch path (see batch_frames): read the source's
    # burned-in hook before the builder below looks for one. Local OCR, no
    # quota, and a no-op once a reel has a hook or when its file is gone. Until
    # this ran here, the one-click path was the only prompt that could go out
    # with no visual grounding at all.
    _ensure_on_screen_hooks([item_id])
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
        hook_lines = _on_screen_hook_lines(item)
        fields = [
            f"Account: {account.name if account else '(none)'}",
            f"Niche: {niche_label or '(none)'}",
            f"Tone: {account.writing_tone if account and account.writing_tone else '(none)'}",
            f"Target audience: {account.target_audience if account and account.target_audience else '(none)'}",
            f"Hook style: {account.hook_style if account and account.hook_style else '(none)'}",
            f"Banned phrases: {account.banned_phrases if account and account.banned_phrases else '(none)'}",
            f"Title rules: {account.title_style_notes if account and account.title_style_notes else '(none)'}",
            f"Caption rules: {account.caption_style_notes if account and account.caption_style_notes else '(none)'}",
            f"Scraped caption line (loose descriptive prose from the repost, NOT their hook - context only, often narrower than the clip, and never a model for the title): {item.title or '(none)'}",
            f"Source URL: {item.source_url or '(none)'}",
            f"Source description: {item.source_description or '(none)'}",
            *hook_lines,
        ]
        transcript_text = (item.transcript_text or "").strip()
        vision_raw = (item.smart_vision_payload or "").strip()
        clip_premise = str(settings.get("clip_premise") or "").strip()
        # Does this reel say anything at all about itself? A chat assistant
        # cannot open the local path, so one with every field empty arrives
        # blind and the only honest reply it can give is to ask what is in the
        # clip. Item 2371 went out that way: Apify returned a metadata-less row
        # in June, nothing downstream filled it, and the copied prompt read
        # "(none)" on every context line. The batch path cannot hit this - it
        # runs the vision pass and attaches a contact sheet - so the guard is
        # only on this one.
        has_signal = any(
            (
                (item.title or "").strip(),
                (item.source_description or "").strip(),
                transcript_text,
                vision_raw,
                bool(hook_lines),
                clip_premise,
            )
        )
        transcript = transcript_text or "(none; inspect the local video)"
        vision_text = vision_raw or "(none)"
    if not has_signal:
        raise DraftRevisionError(
            "This reel has no caption, description, transcript, on-screen hook or "
            "visual evidence, so the prompt would say nothing about the clip and a "
            "chat assistant cannot open the local file. Use Prepare batch instead "
            "(it runs the vision pass and writes a contact sheet to attach), or "
            "describe the clip in Clip premise first."
        )
    title_style, caption_style, title_length = _resolved_styles(account, settings)
    caption_mode = _caption_mode(settings)
    # Same rule source as the live Groq prompt (see effective_title_rules
    # docstring) so the chat path and the API path never drift apart.
    title_rules = _effective_title_rules(
        title_style,
        caption_style,
        niche_label,
        few_shot_winners=few_shot_winners,
        title_length=title_length,
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
            *_style_header_lines(title_style, caption_style, title_length),
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
            "web): work from the signals above — source title, source description, "
            "visual evidence JSON, and transcript — plus verified web research per the "
            "Research rules below.",
            "",
            *_WEB_RESEARCH_LINES,
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
            (
                "Generate 3 meaningfully different on-screen title options and 3 caption options."
                if caption_mode == CAPTION_MODE_PER_OPTION
                else "Generate 3 meaningfully different on-screen title options and exactly ONE "
                "shared caption that works under any of them."
            ),
            "Keep display titles plain text. Only use internal **keyword** emphasis when the Cinema Bold Keywords style requires it.",
            "Never write em dashes or double hyphens ('--') in titles or captions; "
            "use a comma, period, or colon instead. Long dashes read as AI-generated copy.",
            *(
                [
                    _RECOMMENDER_DEFERRAL,
                    "The recommended title and caption MUST share the same option number: the "
                    "app applies title+caption as ONE unit, so rearrange your options before "
                    "returning until the strongest pair sits together (never recommend Title 2 "
                    "+ Caption 3).",
                ]
                if caption_mode == CAPTION_MODE_PER_OPTION
                else [
                    _RECOMMENDER_DEFERRAL,
                ]
            ),
            "Do not invent unsupported facts.",
            "",
            "Automatic NicheFlow handoff for Codex or Claude Code:",
            f"- After generating, save the options directly to item {item_id} in SQLite: "
            "write the draft JSON to a UTF-8 file, then run:",
            f"  .venv\\Scripts\\python.exe scripts\\nicheflow_drafts.py save --item-id {item_id} --file <draft-json-path>",
            "- Do NOT pipe the JSON through Get-Content into --stdin: PowerShell re-decodes "
            "the bytes and silently corrupts em dashes and emoji before Python sees them.",
            *(
                []
                if caption_mode == CAPTION_MODE_PER_OPTION
                else [
                    "- caption_options may hold a SINGLE shared caption even with three titles; "
                    "it is fanned out across the options on save."
                ]
            ),
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
            *_field_disambiguation_lines(caption_style, caption_mode),
            "",
            "Niche fit check (MANDATORY): compare the clip's actual subject to the "
            f"account niche ('{niche_label or 'none set'}'). Before 'Title Option 1:', "
            "write exactly one line:",
            "Niche check: fits",
            "or",
            "Niche check: OFF-NICHE, <one short reason>",
            "Flag borderline or unclear fits as OFF-NICHE so the user decides; still "
            "write the three options either way.",
            "",
            "Return format (write every section header exactly as shown, plain text — "
            "never bold or markdown-formatted; '**Title Option 1:**' breaks the importer):",
            "Niche check: <fits | OFF-NICHE, reason>",
            *_return_format_lines(caption_mode),
            "Selection Notes:",
            "Option 1:",
            "Option 2:",
            "Option 3:",
        ]
    )


def build_clip_title_prompt(
    *,
    account_id: int,
    clip_words: str,
    surrounding_context: str | None = None,
    source_title: str | None = None,
    video_path: str | None = None,
    range_label: str | None = None,
    title_style: str | None = None,
    title_length: str | None = None,
) -> str:
    """A copy-paste prompt asking a chat model for this clip's title options.

    The Clip Studio counterpart to :func:`build_chat_prompt`. Same title rules
    (:func:`smart_drafts.effective_title_rules`) and the same section headers the
    Processing paste parser already reads, so one parser serves both paths and
    the chat route cannot drift from the API route.

    Titles only: a Clip Studio caption is composed from the campaign template
    (``services.campaigns.build_caption``), not written by a model, so asking for
    captions here would produce text nothing consumes.

    The clip's own words and the surrounding minute are given as SEPARATE
    sections on purpose. The model needs the wider context to know what the clip
    is about, but the title has to be delivered by the clip itself — stating
    which is which is what keeps a title off things the viewer never sees.
    """
    context_lines, rule_lines = _clip_title_prompt_parts(
        account_id=account_id,
        source_title=source_title,
        video_path=video_path,
        title_style=title_style,
        title_length=title_length,
    )
    return "\n".join(
        [
            "Write on-screen title options for ONE short clip cut from a longer video.",
            "",
            *context_lines,
            f"Clip position in the source: {range_label or '(unknown)'}",
            "",
            "WHAT THE CLIP ITSELF SAYS (the title must be delivered by THIS):",
            (clip_words or "").strip()[:4000] or "(no speech in this window)",
            "",
            "SURROUNDING CONTEXT (what the clip is about; do NOT claim anything from "
            "here that the clip above does not deliver):",
            (surrounding_context or "").strip()[:6000] or "(none)",
            "",
            *rule_lines,
            "",
            "Return EXACTLY these section headers as PLAIN TEXT, never bold or "
            "prefixed — the app parses them with a line-start match and any prefix "
            "silently breaks the whole import:",
            "Title Option 1:",
            "Title Option 2:",
            "Title Option 3:",
            "Recommended Pick: Title Option N",
            "Why:",
        ]
    )


def _clip_title_prompt_parts(
    *,
    account_id: int,
    source_title: str | None,
    video_path: str | None = None,
    title_style: str | None,
    title_length: str | None,
) -> tuple[list[str], list[str]]:
    """``(account/source context, rule block)`` shared by both clip prompts.

    Split out because the batch prompt needs the same rules stated once for many
    clips. Deriving them by slicing the single-clip prompt's text is what an
    earlier version did, and it silently dropped every rule — they sit *after*
    the per-clip material, so the slice kept only the account header.
    """
    with get_session() as session:
        account = session.get(Account, int(account_id))
        if account is None:
            raise DraftRevisionError(f"No account with id {account_id}.")
        niche_label = account.niche_label or None
        account_key = account.instagram_handle
        fields = [
            f"Account: {account.name}",
            f"Niche: {niche_label or '(none)'}",
            f"Tone: {account.writing_tone or '(none)'}",
            f"Target audience: {account.target_audience or '(none)'}",
            f"Hook style: {account.hook_style or '(none)'}",
            f"Banned phrases: {account.banned_phrases or '(none)'}",
            f"Title rules: {account.title_style_notes or '(none)'}",
        ]
        try:
            prefs = json.loads(account.processing_preferences or "{}")
        except ValueError:
            prefs = {}
        if not isinstance(prefs, dict):
            prefs = {}
    few_shot_winners = top_titles_for_account(account_key) if account_key else []
    resolved_style = title_style or prefs.get("prompt_title_style") or None
    resolved_length = title_length or prefs.get("title_length") or "long"
    title_rules = _effective_title_rules(
        resolved_style,
        None,
        niche_label,
        few_shot_winners=few_shot_winners,
        title_length=resolved_length,
    )
    context_lines = [
        *(
            ["Local clip file (inspect the frames if you can open it):", f'"{video_path}"', ""]
            if video_path
            else []
        ),
        "Account context:",
        *fields,
        f"Title style: {resolved_style or 'Auto'}",
        *(
            []
            if resolved_style == TITLE_STYLE_BEATS_V2
            else [f"Title length: {str(resolved_length).title()}"]
        ),
        "",
        f"Source video title: {source_title or '(unknown)'}",
    ]
    rule_lines = [
        *_WEB_RESEARCH_LINES,
        "",
        "On-screen title rules (follow these exactly):",
        *title_rules,
        "",
        "Hook framing (drama is allowed, overclaiming is not):",
        *smart_drafts._hook_drama_and_fact_safety_rules(),
        "",
        *_FACT_CHECK_LINES,
        "",
        "Generate 3 meaningfully different on-screen title options per clip.",
        "Keep display titles plain text.",
        "Never write em dashes or double hyphens ('--'); use a comma, period, or "
        "colon instead. Long dashes read as AI-generated copy.",
        "Do not invent unsupported facts.",
        _RECOMMENDER_DEFERRAL,
    ]
    return context_lines, rule_lines


# A clip header in a batch reply. Same shape as _REEL_HEADER (tolerant of
# markdown, "#", and trailing labels) but keyed on CLIP NUMBER: Clip Studio
# candidates have no DownloadItem id to echo — they only exist in the batch.
_CLIP_HEADER = re.compile(
    r"^\s*(?:[*_#=]+\s*)?clip\s*#?\s*(\d+)\s*(?:[:|)\-=#*_].*)?$",
    re.IGNORECASE,
)


def build_clip_batch_title_prompt(
    *,
    account_id: int,
    clips: list[dict],
    source_title: str | None = None,
    title_style: str | None = None,
    title_length: str | None = None,
) -> str:
    """One prompt covering every candidate in the batch.

    Writing titles one clip at a time means one copy, one paste and one model
    round trip per candidate — eight of each for a normal batch. The rules are
    identical for every clip in it, so they are stated once and each clip
    contributes only its own words.

    Each ``clips`` entry needs ``number`` (1-based, the routing key), ``words``
    (what the clip says) and optionally ``context`` and ``range_label``.
    """
    if not clips:
        raise DraftRevisionError("No clips to write titles for.")
    context_lines, rule_lines = _clip_title_prompt_parts(
        account_id=account_id,
        source_title=source_title,
        title_style=title_style,
        title_length=title_length,
    )

    blocks: list[str] = []
    for clip in clips:
        number = int(clip.get("number") or 0)
        words = str(clip.get("words") or "").strip()
        context = str(clip.get("context") or "").strip()
        blocks.extend(
            [
                "",
                f"Clip {number}: {clip.get('range_label') or ''}".rstrip(),
                "What this clip says (the title must be delivered by THIS):",
                words[:2500] or "(no speech in this window)",
                *(
                    ["Surrounding context (do NOT claim what the clip does not deliver):",
                     context[:1500]]
                    if context
                    else []
                ),
            ]
        )

    return "\n".join(
        [
            "Write on-screen title options for SEVERAL short clips cut from one "
            "longer video. The rules below apply to every clip.",
            "",
            *context_lines,
            "",
            *rule_lines,
            "",
            f"There are {len(clips)} clips below. Write titles for EVERY one.",
            *blocks,
            "",
            "Return EXACTLY this shape, repeated once per clip, with PLAIN TEXT "
            "headers — never bold or prefixed, because the app matches them at "
            "line start and any prefix silently breaks the import:",
            "",
            "Clip 1:",
            "Title Option 1:",
            "Title Option 2:",
            "Title Option 3:",
            "Recommended Pick: Title Option N",
            "Why:",
            "",
            "Clip 2:",
            "... and so on for every clip, in order, using the same clip numbers "
            "given above.",
        ]
    )


def parse_clip_batch_titles(text: str) -> dict[int, PastedDraft]:
    """Split a batch reply into ``{clip number: parsed titles}``.

    Routing is by the echoed clip number rather than by position, so a reply that
    reorders the clips, or omits one, still lands every block on the right
    candidate instead of silently shifting them all by one.
    """
    lines = (text or "").splitlines()
    blocks: dict[int, list[str]] = {}
    current: int | None = None
    for line in lines:
        match = _CLIP_HEADER.match(line)
        if match:
            current = int(match.group(1))
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(line)
    parsed: dict[int, PastedDraft] = {}
    for number, body in blocks.items():
        draft = parse_pasted_draft("\n".join(body))
        if draft.title_options:
            parsed[number] = draft
    return parsed


def _batch_item_delimiter(index: int, seq: int | None, account_name: str | None = None) -> str:
    # Header carries the reel number (which models reproduce reliably) plus the
    # account-relative "#id" the user sees in the list. Import prefers the #id
    # when it survives and falls back to the reel number otherwise, and it
    # validates the #id against this batch — so a dropped or garbled id never
    # misroutes (the failure that retired the older id-in-header scheme).
    # The account name rides AFTER the closing "=====" on purpose. _REEL_HEADER
    # already tolerates trailing text there, so routing still keys off the (#id)
    # even if a model paraphrases or drops the name - it is a label for the
    # operator matching a prepared row to the chat window it belongs to, never a
    # routing key. Putting it INSIDE the bars ("REEL 1 (#401) Beneath History
    # =====") does not parse.
    suffix = f" {account_name}" if account_name else ""
    if seq is not None:
        return f"===== REEL {index} (#{seq}) ====={suffix}"
    return f"===== REEL {index} ====={suffix}"


# Every prompt on this path carries a style-specific "RECOMMENDED PICK" rule
# inside its title-rules block. The batch headers used to ALSO carry their own
# global version ("recommend the one whose specific the clip delivers most
# strongly"), and the two competed: after the HistoryTrails rules were retuned
# so the reactive register wins by default, a real batch still recommended the
# documentary option in 6 of 6 reels and justified every one of them as "the
# strongest concrete signal", which is the global line's wording, not the style
# rule's. The header now defers instead of instructing, so the style rule is the
# only voice on this decision.
# An objection used to have nowhere to go. The block contract says "one block per
# reel, one for every reel" and forbids skipping, but named no way to REGISTER a
# problem, so a model with a real one (a source caption that misidentifies
# somebody, a clip it thinks is off-niche or unsafe) answered in prose outside the
# format instead. The block vanished, the import reported "1 unmatched", and the
# reel reached Finish Batch with no draft, where it exports under its raw scraped
# caption. A concern must cost a line inside the block, never the block.
_CONCERN_ESCAPE_HATCH = (
    "If you have a problem with a reel (the source caption looks wrong, the "
    "footage does not support the story, or the clip seems off-niche), still "
    "return its block and put the problem on a 'Concern:' line inside it. You "
    "may write no options for that reel; the operator reads Concern lines and "
    "rejects those reels. A missing block is worse, because the reel reaches "
    "export with its raw scraped caption as the on-screen title."
)

_RECOMMENDER_DEFERRAL = (
    "Recommend one option per reel using the RECOMMENDED PICK rule in the "
    "on-screen title rules above; that rule is tuned per account. Only options "
    "the evidence supports are eligible. Add one short selection note per option."
)

# The title rules police variety WITHIN a reel's three options, and nothing used
# to police it ACROSS reels. A real six-reel batch came back with four titles
# opening "Would you ..." / "Would this ...", because one prompt draws one set of
# examples and every reel is written against it. Cross-reel variety therefore has
# to be stated where the batch itself is described.
_BATCH_VARIETY_RULES = (
    "Cross-reel variety (HARD RULE): the reels in this batch are written against "
    "one shared set of rules and examples, which pulls every reel toward the same "
    "sentence. Do NOT let that happen.",
    "- Never open two reels' titles with the same first three words. If you have "
    "already written 'Would you ...' once in this batch, the next question must "
    "take a different shape ('How did ...', 'Which ...', 'Remember ...', 'What "
    "happens when ...').",
    "- Across the batch, question openers must vary and first-person openers must "
    "vary. Repeating one stem is the single most common failure here.",
    "- Vary the RECOMMENDED register across reels too. A batch that recommends "
    "the same register, or the same option number, for every reel has not "
    "actually chosen: it has applied a default. Over a batch of four or more "
    "reels, no single register may take more than about half the "
    "recommendations, and the option number you recommend must change between "
    "reels. Pick per clip, then check the spread before you return.",
)

# Appended to each reel's attachment line. The attached image is no longer one
# still: it is a grid of moments sampled at the clip's scene cuts, so the model
# has to be told to read it as a sequence or it describes the first tile and
# stops. Kept short because it repeats once per reel in the batch prompt.
_CONTACT_SHEET_HINT = (
    "(a contact sheet: several moments from this one clip, in time order, "
    "left to right then top to bottom. Read every tile before writing.)"
)

# The visual evidence JSON is now the PRIMARY visual grounding on the batch
# path (it replaced the per-reel image attachment), so the prompt has to say
# what its fields mean. Without this the model treated the JSON as loose
# metadata and kept writing from the source caption alone, which is exactly the
# off-clip drift the vision pass exists to prevent.
_VISUAL_EVIDENCE_GUIDANCE = [
    "How to use the Visual evidence JSON (this is what is actually on screen):",
    "- It comes from a vision model that read up to 5 frames sampled across the "
    "clip, so it describes the WHOLE clip, not one moment. Treat it as your eyes.",
    "- 'ocr_text' and 'on_screen_hook' are text burned into the video. The hook is "
    "often the real premise of the clip; the title you write must not contradict it.",
    "- 'main_subject', 'main_action', and 'scene_summary' are what the viewer sees. "
    "A title describing something absent from these is off-clip: rewrite it.",
    "- 'referenced_entity' / 'referenced_concept' name the movie, person, game, or "
    "meme format the clip depends on. Use them; do not guess a different one.",
    "- 'confidence' and 'uncertainty_notes' bound how specific you may be. On low "
    "confidence, stay with what the source caption supports.",
    # This used to end "and stay more conservative", which fired on EVERY reel:
    # the vision pass is Groq-backed, no key is configured, and 0 of 185 items on
    # the live account carry a payload. So the prompt was issuing a blanket
    # instruction to hedge on 100% of clips, while the contact sheet and the
    # source's own OCR'd hook sat in the same block as real evidence. Timid
    # output was being asked for, not merely tolerated.
    "- When the JSON is '(none)', no vision pass ran for that reel. That is the "
    "normal case, not a degraded one, and it is NOT a reason to write timidly: "
    "the attached contact sheet, the source caption, and the source account's "
    "own on-screen hook when one is given are real evidence. Be conservative "
    "about SPECIFICS the evidence cannot carry. Do not be conservative about the "
    "writing.",
]


def _account_prompt_header(
    account: Account | None,
    *,
    settings: dict,
    item_ids: list[int],
    attachments_expected: bool = False,
) -> list[str]:
    niche_label = account.niche_label if account and account.niche_label else None
    caption_mode = _caption_mode(settings)
    account_key = account.instagram_handle if account is not None else None
    few_shot_winners = top_titles_for_account(account_key) if account_key else []
    title_style, caption_style, title_length = _resolved_styles(account, settings)
    title_rules = _effective_title_rules(
        title_style,
        caption_style,
        niche_label,
        few_shot_winners=few_shot_winners,
        title_length=title_length,
    )
    hook_rules = smart_drafts._hook_drama_and_fact_safety_rules()
    # Same source as the live CAPTION block (see effective_caption_rules); the
    # batch path used to emit only a bare "Caption style:" token, so the web
    # model had no length/structure target and returned one-line captions.
    caption_rules = smart_drafts.effective_caption_rules(
        caption_style,
        account_voice=_caption_rule_voice(account),
    )
    return [
        "You are drafting Instagram Reel titles and captions for one NicheFlow account.",
        "This is for ChatGPT or Claude web.",
        *(
            [
                "Some reels below ask you to attach and inspect a contact sheet (a grid of "
                "moments from that one clip); the rest "
                "carry their visual evidence as JSON instead."
            ]
            if attachments_expected
            else [
                "No images are attached: each reel's visual evidence is provided as JSON "
                "in its context block."
            ]
        ),
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
        *_style_header_lines(title_style, caption_style, title_length),
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
        *_VISUAL_EVIDENCE_GUIDANCE,
        "",
        *_SOURCE_CAPTION_GUIDANCE,
        "",
        *_FACT_CHECK_LINES,
        "",
        *_WEB_RESEARCH_LINES,
        "",
        "Plain-text rules:",
        "- Keep display titles plain text.",
        "- Do not use markdown formatting in section headers or titles.",
        "- Never write em dashes or double hyphens ('--') in titles or captions.",
        "- Do not invent unsupported facts.",
        (
            "- The recommended title and caption MUST share the same option number."
            if caption_mode == CAPTION_MODE_PER_OPTION
            else "- Write THREE title options and exactly ONE 'Shared Caption:' per reel."
        ),
        f"- {_RECOMMENDER_DEFERRAL}",
        "",
        *_BATCH_VARIETY_RULES,
        "",
        "Return one block per reel, one for every reel, in order.",
        "START each reel's block with its own header line that is exactly:",
        "===== REEL <n> (#<id>) ===== <account name>",
        "copying the whole line exactly as shown in that reel's context below (for "
        "example '===== REEL 1 (#143) ===== Past Moments Daily'). The (#id) is how the "
        "app files each reply to the correct video, so never change, drop, or invent it. "
        "The account name after the bars is what lets a human tell which batch this "
        "reply belongs to, so echo it too.",
        "Do not rename, renumber, merge, or skip any reel, even if two clips look similar.",
        _CONCERN_ESCAPE_HATCH,
        "",
        "Niche fit check (per reel, MANDATORY): compare the clip's actual subject to "
        f"the account niche ('{niche_label or 'none set'}'). Directly under the reel's "
        "header line, before 'Title Option 1:', write exactly one line:",
        "Niche check: fits",
        "or",
        "Niche check: OFF-NICHE, <one short reason>",
        "Flag borderline or unclear fits as OFF-NICHE so the user decides. Still "
        "draft all three options either way; the app imports flagged reels normally.",
        "",
        *_field_disambiguation_lines(settings.get("caption_style") or None, caption_mode),
        "Inside each block, use this exact plain-text format:",
        "Niche check: <fits | OFF-NICHE, reason>",
        *_return_format_lines(caption_mode),
        "Selection Notes:",
        "Option 1:",
        "Option 2:",
        "Option 3:",
    ]


def _item_account_name(item: DownloadItem) -> str | None:
    """The item's account name, for labelling its reel header.

    Uses the item's own session so this adds no connection handling to callers,
    exactly like ``_on_screen_hook_lines`` above.
    """
    session = object_session(item)
    if session is None or item.account_id is None:
        return None
    account = session.get(Account, item.account_id)
    return (account.name or None) if account else None


def _batch_item_context(index: int, item: DownloadItem, seq: int | None) -> list[str]:
    """One reel's context block.

    Vision-first: when the item carries visual-evidence JSON (from the Groq
    vision pass over up to 5 sampled frames, including burned-in on-screen
    text), that JSON IS the visual grounding and no image is attached. An
    attached still is ~1.5k tokens and shows one moment; the JSON is ~100 and
    covers the whole clip. Items whose vision pass could not run still ask for
    the frame, so a missing Groq key degrades to the old behavior instead of
    leaving the model blind.
    """
    transcript = (item.transcript_text or "").strip() or "(none)"
    vision_text = (item.smart_vision_payload or "").strip()
    frame_name = f"reel_{index}_item{item.id}.jpg"
    return [
        _batch_item_delimiter(index, seq, _item_account_name(item)),
        *(
            []
            if vision_text
            else [f"Attach and inspect image file: {frame_name} {_CONTACT_SHEET_HINT}"]
        ),
        f"Scraped caption line (loose descriptive prose from the repost, NOT their hook - context only, often narrower than the clip, and never a model for the title): {item.title or '(none)'}",
        f"Source URL: {item.source_url or '(none)'}",
        f"Source description: {item.source_description or '(none)'}",
        *_on_screen_hook_lines(item),
        "",
        "Visual evidence JSON:",
        vision_text[:4000] or "(none)",
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
        _PROMPT_SEQ_TO_ITEM[frozenset(ids)] = {
            seq_by_item[item_id]: item_id for item_id in ids if item_id in seq_by_item
        }
        # Only reels WITHOUT visual evidence still need an attached still, so
        # the header's attachment instruction has to match what the reel blocks
        # below actually ask for.
        attachments_expected = any(
            not (item.smart_vision_payload or "").strip() for item in ordered
        )
        lines = _account_prompt_header(
            account,
            settings=settings,
            item_ids=ids,
            attachments_expected=attachments_expected,
        )
        lines.extend(["", "Reel contexts:"])
        for index, item in enumerate(ordered, start=1):
            lines.extend(["", *_batch_item_context(index, item, seq_by_item.get(item.id))])
    return "\n".join(lines)


# Above this many differing rule lines, an account is treated as having its own
# rules rather than a delta on the shared block (see build_multi_account_batch_chat_prompt).
_MAX_RULE_DELTA_LINES = 6


def _account_voice_delta(account: Account | None, *, item_count: int) -> list[str]:
    """The per-account fields that actually differ between accounts.

    The style RULES (title/hook/caption blocks, ~14k characters) are emitted
    once for the whole batch when every account resolves to the same ones,
    which is the normal case for a niche network. Only this short voice block
    repeats per account.
    """
    return [
        f"Account: {account.name if account else '(none)'}",
        f"Niche: {account.niche_label if account and account.niche_label else '(none)'}",
        f"Tone: {account.writing_tone if account and account.writing_tone else '(none)'}",
        f"Target audience: {account.target_audience if account and account.target_audience else '(none)'}",
        f"Hook style: {account.hook_style if account and account.hook_style else '(none)'}",
        f"Banned phrases: {account.banned_phrases if account and account.banned_phrases else '(none)'}",
        f"Title rules: {account.title_style_notes if account and account.title_style_notes else '(none)'}",
        f"Caption rules: {account.caption_style_notes if account and account.caption_style_notes else '(none)'}",
        f"Reels for this account: {item_count}",
    ]


def _on_screen_hook_lines(item: DownloadItem) -> list[str]:
    """The source account's own burned-in hook, when we have OCR'd it.

    This is the single most useful line in the block and it was missing until
    now. The scraped caption is descriptive prose, so a drafter working from it
    alone writes a description: a real batch produced "A woman in bright pink
    loses her footing on the stairs as the dog behind her closes the gap" while
    the source's own on-screen line for the same clip read "The Breed (2006)
    features a scene so accidentally hilarious it completely steals the movie".
    One describes the footage, the other says why it is worth watching.

    Framed as an angle to match and NOT a text to copy, for three reasons: the
    same clip under the same wording is a duplicate-content signal, the claims
    in it are the reposter's and unverified (see the asserted_signals_text
    channel in draft_guard), and five accounts posting one reel under one hook
    would be indistinguishable.

    Returns [] when the reel has no OCR'd title, which is most of them today
    (coverage is 14% of scraped candidates), so the block simply omits the line.
    """
    # Lives on ScrapeCandidate (keyed by video_id), not on the item. Uses the
    # item's own session so this adds no connection handling to the callers.
    video_id = (item.video_id or "").strip()
    session = object_session(item)
    if not video_id or session is None:
        return []
    hook = (
        session.query(ScrapeCandidate.on_screen_title)
        .filter(ScrapeCandidate.video_id == video_id)
        .filter(ScrapeCandidate.on_screen_title.is_not(None))
        .limit(1)
        .scalar()
    )
    hook = " ".join((hook or "").split())
    if not hook:
        return []
    return [
        "SOURCE ACCOUNT'S OWN ON-SCREEN HOOK (read off their video - this is the "
        "angle that actually earned the views on their account, so MATCH THE "
        "ANGLE AND THE REGISTER: what it treats as the draw, and whether it is a "
        "verdict, a reaction or a plain statement. Write your own line on that "
        "angle. Do NOT reuse its wording, and do NOT inherit its factual claims "
        "unless the evidence above supports them independently): "
        f"{hook}",
    ]


# Without this the three options come back at whatever length the writer
# gravitates to, and the operator picks from one band with no alternative. A
# real batch (2026-09-11) returned five reels for one account with ten long
# options and five medium ones, no short option anywhere, and every pick landed
# long - that account's worst measured band. The rule does not prescribe a
# length, it only guarantees the choice exists; scripts/title_length_report.py
# batch reports when a batch violates it anyway.
_LENGTH_SPREAD_RULE = (
    "- SPREAD THE THREE OPTIONS ACROSS LENGTHS. Inside the ceiling above, the "
    "options must not all come back the same size: write at least one at 60 "
    "characters or fewer, and at least one over 100, with the third wherever "
    "the clip wants it. Length follows the beats you choose, so this is a "
    "check on those, not licence to pad one option or gut another. If the clip "
    "genuinely only supports one length, say so in that reel's selection notes "
    "rather than stretching a line to fill a band."
)


def _effective_title_rules(
    title_style: str | None,
    caption_style: str | None,
    niche_label: str | None,
    *,
    few_shot_winners: list[str] | None = None,
    title_length: str | None = None,
) -> list[str]:
    """The on-screen title rules for a batch prompt.

    v2 (``processing.title_lab.production_title_rules``) replaces the 2,534-word
    historytrails block with ~940 words. What changed, and why, in order of
    measured effect on this account's own 726-title reference set:

    * BEATS. A title moving through three or more beats - setup, what happened,
      the turn, someone's reaction - outperforms one that states a single thing,
      and the gap holds inside every length band (1-2 beats sits at 63-73k
      regardless of length; 3+ reaches 150-350k). The old block had no such rule
      and asked for word counts instead, which is why its titles came back as
      one long statement.
    * SINGULAR "I" IS NO LONGER BANNED OUTRIGHT... it is still banned, but for
      the right reason. The old rule's stated justification (an "I" claims the
      on-screen action) did not survive testing: what unbanning it actually
      produced was the account speaking as a person with opinions about a real
      person on screen. The ban stays; the register it was blamed for came from
      the option matrix, not from the ban.
    * The specificity matrix, the length bands, and the per-option measured
      medians are gone. They were unsatisfiable as a set - a real batch broke
      the block's own "at least one option carries no specific" hard rule in
      6 of 6 reels - and the numbers they cited no longer match the data.
    * A 130-character ceiling replaces "18-24 words", because the constraint is
      the renderer, not reach: ``_fit_title_overlay`` gives up past four lines at
      the font floor and dumps the remainder onto one line that runs off-frame.

    Selected per account through the Title Style dropdown rather than a global
    switch, so one account can run v2 while the rest stay on their current
    rules - which is what makes a real A/B possible instead of an all-or-nothing
    cutover.

    ``title_length``/``few_shot_winners`` are accepted. Under v2 the length
    control is replaced by the character ceiling, with one exception: "short"
    adds a hard 70-character rule on top. That exists for a per-account A/B -
    this account's own reels measured the other way from the reference set (in
    June 2026, titles of 60 characters or fewer hit 100k+ views 17% of the time
    against 7% for longer ones), and a two-arm test settles it where neither
    dataset can. The draft guard enforces the same limit on the reply, because
    a prompt length rule alone was ignored in August.
    """
    if title_style in (TITLE_STYLE_BEATS_V2, TITLE_STYLE_BEATS_V3):
        rules = title_lab.production_title_rules(
            few_shot_winners=few_shot_winners,
            reaction_ending=title_style == TITLE_STYLE_BEATS_V3,
        )
        limit = draft_guard.title_char_limit(title_length)
        if limit is None:
            return [*rules, _LENGTH_SPREAD_RULE]
        return [
            *rules,
            f"- SHORT TITLES FOR THIS ACCOUNT, OVERRIDING THE 130-CHARACTER CEILING: "
            f"every title option must be at most {limit} characters including "
            f"spaces, which is two lines on screen. Count them. Keep the single "
            f"strongest beat, usually the payoff, and name the person or thing. "
            f"Drop the setup rather than squeezing every beat in.",
        ]
    return smart_drafts.effective_title_rules(
        title_style,
        caption_style,
        niche_label,
        few_shot_winners=few_shot_winners,
        title_length=title_length,
    )


def _style_header_lines(
    title_style: str | None, caption_style: str | None, title_length: str | None
) -> list[str]:
    """The Caption/Title style lines for a prompt header, from the RESOLVED styles.

    These lines used to read ``settings`` directly while the rules below them
    read the account's saved preferences, so a batch screen that sends ``{}``
    (it has no style picker) printed "Title style: Auto (match caption style)"
    and "Title length: Long" above a v2 rulebook that was neither. The header
    is what a human reads to check the prompt is right, so it has to agree with
    the rules it sits above.

    Title length is omitted under v2 unless it is one the rules honour, because
    v2 replaces the length control with its own ceiling (see
    ``_effective_title_rules``) and printing an ignored setting is the same bug
    in the other direction.
    """
    lines = [
        f"Caption style: {caption_style or '(none)'}",
        f"Title style: {title_style or 'Auto (match caption style)'}",
    ]
    honoured = title_style not in (
        TITLE_STYLE_BEATS_V2,
        TITLE_STYLE_BEATS_V3,
    ) or draft_guard.title_char_limit(title_length)
    if honoured:
        lines.append(f"Title length: {str(title_length or 'long').title()}")
    return lines


def _resolved_styles(account: Account | None, settings: dict) -> tuple[str | None, str | None, str]:
    """The styles a prompt should actually use: caller's choice, else the account's.

    Every builder needs this and only the multi-account one had it, so the other
    two silently produced a DIFFERENT rulebook for the same account. The live
    accounts are saved as ``beats_v2``; an empty ``settings`` resolved
    ``title_style=None``, which falls past the v2 branch to the v1 rules. So the
    Processing screen's single-clip prompt and the single-account batch were
    handing out Curiosity / Open Loop while the batch screen handed out v2. Same
    account, same clip, two different sets of instructions.
    """
    prefs: dict = {}
    if account is not None:
        try:
            loaded = json.loads(account.processing_preferences or "{}")
        except ValueError:
            loaded = {}
        if isinstance(loaded, dict):
            prefs = loaded
    return (
        settings.get("title_style") or prefs.get("prompt_title_style") or None,
        settings.get("caption_style") or prefs.get("caption_style") or None,
        settings.get("title_length") or prefs.get("title_length") or "long",
    )


def _account_style_rules(account: Account | None, settings: dict) -> list[str]:
    """The style-rule block for one account (title + hook + caption rules).

    Styles fall back to the ACCOUNT's saved processing_preferences when the
    caller does not name one. The multi-account batch screen sends ``{}`` (it
    has no per-account style picker), so without this every account resolved
    title_style=None and dropped to the caption-derived rules -- meaning the
    historytrails_record rule set the accounts are actually configured for,
    including the register mix and the specificity rules, never reached the
    prompt the operator pasted. The single-clip path already reads these same
    preference keys (see draft_generation.generate_titles_for_clip).
    """
    niche_label = account.niche_label if account and account.niche_label else None
    account_key = account.instagram_handle if account is not None else None
    few_shot_winners = top_titles_for_account(account_key) if account_key else []
    title_style, caption_style, title_length = _resolved_styles(account, settings)
    return [
        "On-screen title rules (follow these exactly):",
        *_effective_title_rules(
            title_style,
            caption_style,
            niche_label,
            few_shot_winners=few_shot_winners,
            title_length=title_length,
        ),
        "",
        "Hook framing (drama is allowed, overclaiming is not):",
        *smart_drafts._hook_drama_and_fact_safety_rules(),
        "",
        "Caption rules (follow these exactly):",
        *smart_drafts.effective_caption_rules(
            caption_style,
            account_voice=_caption_rule_voice(account),
        ),
    ]


# Review states that take a reel out of the drafting queue. "rejected" is the
# per-account reject; "blocked" is the global one (library.reject_item_globally),
# which already hides the reel from the Processing table — offering it for a
# batch draft would resurrect a clip the user killed everywhere else.
#
# Public because a batch's item list is PINNED when the prompt is prepared, so
# every later stage (import here, plan/finish in batch_finish) has to re-check
# this rather than trusting the candidate list it was built from.
UNDRAFTABLE_REVIEW_STATES = frozenset({"rejected", "blocked"})


def has_applied_draft(item: DownloadItem) -> bool:
    """Whether a draft is live ON THE ITEM, as opposed to merely imported.

    A ``DraftRevision`` row only means a batch reply was parsed into options;
    ``draft_revisions.apply_revision`` (which ``batch_finish`` calls) is what
    copies a chosen option onto ``title_draft``/``caption_draft``. That pair is
    what export and publish read and what the Processing table calls "Draft", so
    it is the only honest answer to "does this reel have a draft yet".
    """
    return bool((item.title_draft or "").strip() or (item.caption_draft or "").strip())


def batch_candidates(
    *,
    niche: str | None = None,
    per_account: int = 6,
    account_ids: list[int] | None = None,
) -> list[dict]:
    """Publishable reels per account, ready for a cross-account batch.

    Mirrors the Processing screen's own draftable filter (not posted/skipped,
    neither locally rejected nor globally blocked) so the two surfaces agree on
    what is in play. Resting and flagged accounts are excluded for the same
    reason distribution skips them: their reels should not be queued up for
    posting.

    Rows carry ``has_draft``. Already-drafted reels are INCLUDED, appended after
    the draftless ones, because dropping them made an imported reel disappear
    from this screen on the next poll: redrafting a title meant going to
    Processing, and an import that matched nothing looked exactly like one that
    worked. ``available`` still counts only the DRAFTLESS reels, so the "N of M
    draftless" label keeps its meaning; the caller filters on ``has_draft`` for
    everything else.

    Rows also carry ``needs_finish`` (and the group ``pending_finish``) for the
    reels whose draft was imported but never applied — see the comment on that
    list below. They sit between the draftless reels and the finished ones so
    the per-account limit cannot bury them.
    """
    limit = max(1, int(per_account))
    # Lazy import: keep draft_handoff free of library's downloader import chain.
    from nicheflow_studio.services import batch_finish, library, pooling

    # Distribution creates a Processing row before its shared footage lands, and
    # the only thing that ever fills in the path is a one-shot daemon thread. If
    # that thread died (app closed, transient download failure) the row keeps a
    # NULL file_path forever and this screen reports it as "waiting on footage"
    # on every refresh. Re-linking here costs one batched query and self-heals
    # every row whose footage a sibling account already pulled.
    try:
        pooling.repair_pending_review_media_links()
    except Exception:  # noqa: BLE001 - a repair failure must not blank the screen
        logger.exception("Re-linking pending-review footage before a batch failed.")

    with get_session() as session:
        query = session.query(Account)
        if account_ids:
            query = query.filter(Account.id.in_([int(a) for a in account_ids]))
        if niche:
            query = query.filter(Account.niche == niche)
        accounts = [
            account
            for account in query.all()
            if (account.operational_status or "active") == "active"
        ]

        drafted_item_ids = {
            row[0]
            for row in session.execute(select(DraftRevision.download_item_id).distinct()).all()
        }
        # The "#N" the Processing table shows and the batch prompt/paste-router
        # key on. Surfacing the raw id here instead made the batch list name the
        # same clip by a different number than every other surface.
        seq_by_item = library.account_sequence_map(session)
        # Publishing marks UploadJob, not DownloadItem.status, so filtering on
        # the item's own status alone let already-posted reels back in here as
        # "draftless" while Processing correctly showed them as Posted.
        posted_item_ids = library.already_posted_item_ids(session)
        # ...and match on the source video too, not just on this account's own
        # copy of it, so the legacy direct-download path (no pool item, no
        # assignment row) cannot hand an account footage it already published.
        # Scoped PER ACCOUNT: the pool is shared, so a reel a sibling posted is
        # legitimately on its way out here — the reuse gap spaces those, this
        # only stops one account repeating itself.
        posted_video_ids_by_account = library.posted_source_video_ids_by_account(session)
        # Exports already booked to publish. Same rule batch_finish applies when
        # it refuses to re-render a reel (see _COMMITTED_JOB_STATUSES there).
        committed_item_ids = {
            row[0]
            for row in session.execute(
                select(UploadJob.download_item_id).where(
                    or_(
                        UploadJob.status.in_(batch_finish.COMMITTED_JOB_STATUSES),
                        UploadJob.posted_at.is_not(None),
                    )
                )
            ).all()
        }

        groups: list[dict] = []
        for account in accounts:
            items = (
                session.query(DownloadItem)
                .filter(
                    DownloadItem.account_id == account.id,
                    DownloadItem.status.notin_(["posted", "skipped"]),
                )
                .order_by(DownloadItem.id)
                .all()
            )
            posted_video_ids = posted_video_ids_by_account.get(account.id, frozenset())
            # Everything the account still owes a draft on. Rejected and blocked
            # reels drop out HERE, so a clip the user killed can never come back
            # as a "still downloading" ghost below — retired rows keep no media
            # link, and this is the only place that could resurface them.
            draftable = [
                item
                for item in items
                if item.id not in drafted_item_ids
                and item.id not in posted_item_ids
                and (item.video_id or "") not in posted_video_ids
                and (item.review_state or "") not in UNDRAFTABLE_REVIEW_STATES
            ]
            # A distributed clip gets its pending-review row immediately but its
            # file_path only once the shared asset finishes downloading. Those
            # rows can't be drafted yet, so they stay out of `items`/`available`
            # — but they are reported separately, because silently dropping them
            # is what made a freshly distributed account read "5 of 5" after a
            # 6-clip distribute.
            eligible = [item for item in draftable if item.file_path]
            pending_media = len(draftable) - len(eligible)
            # Reels this account HAS drafted, offered alongside the draftless
            # ones. Importing a draft used to delete the reel from this screen
            # on the next poll, so a title the operator wanted to redo was only
            # reachable from Processing - and an import that silently matched
            # nothing looked identical to one that worked. They are listed, not
            # selected: `has_draft` is what the UI ticks on.
            #
            # COMMITTED reels stay out. Once an export is booked into a slot,
            # handed to the Worker, or published, redrafting its title changes
            # nothing that is going out, and the screen's post-finish reconcile
            # reads "reels that did export drop out of the candidate list" to
            # tell a successful finish from a no-op. Having a draft used to be
            # what removed them; now the commit check has to say so directly.
            listed_drafted = [
                item
                for item in items
                if item.id in drafted_item_ids
                and item.file_path
                and item.id not in committed_item_ids
                and item.id not in posted_item_ids
                and (item.video_id or "") not in posted_video_ids
                and (item.review_state or "") not in UNDRAFTABLE_REVIEW_STATES
            ]
            # Imported but never finished: a revision exists, no option was ever
            # applied. finish_batch only touches the ids it is handed and the
            # Finish button is driven by an in-session plan, so a batch that was
            # imported and then left (app closed, only part of it finished)
            # stranded its leftovers here — counted as drafted, hidden behind the
            # "already drafted" fold, with no surface left that could act on
            # them. 23 reels had piled up that way by 2026-09-22. They are listed
            # FIRST and counted separately: what they need is Finish, not a new
            # draft, so re-prompting them would throw away a draft already paid
            # for.
            pending_finish = [item for item in listed_drafted if not has_applied_draft(item)]
            pending_finish_ids = {item.id for item in pending_finish}
            already_drafted = [item for item in listed_drafted if item.id not in pending_finish_ids]

            def _row(item: DownloadItem) -> dict:
                return {
                    "id": item.id,
                    "account_seq": seq_by_item.get(item.id),
                    "title": item.title,
                    "source_url": item.source_url,
                    # The bridge turns this into a virtual-host preview URL; the
                    # service stays free of the app layer's media mapping.
                    "file_path": item.file_path,
                    "has_draft": item.id in drafted_item_ids,
                    # Drafted, but the draft is still sitting in its revision.
                    "needs_finish": item.id in pending_finish_ids,
                    "has_vision": bool((item.smart_vision_payload or "").strip()),
                    "review_state": item.review_state or None,
                }

            candidates = [
                *map(_row, eligible[:limit]),
                *map(_row, pending_finish[:limit]),
                *map(_row, already_drafted[:limit]),
            ]
            groups.append(
                {
                    "account_id": account.id,
                    "account_name": account.name,
                    "niche": account.niche,
                    "auto_schedules": bool(account.auto_schedule_on_export),
                    "items": candidates,
                    # How many more this account could offer if the per-account
                    # limit were raised — the UI shows "6 of 9" so a short list
                    # reads as a small backlog rather than a bug.
                    "available": len(eligible),
                    # Draftable reels whose footage is still downloading. Shown
                    # so a short list reads as "wait a moment", not "distribute
                    # gave me fewer than I asked for".
                    "pending_media": pending_media,
                    # Reels stuck between Import and Finish, before the limit —
                    # same "N of M" contract as `available`, so a capped list
                    # still reports the true size of the backlog.
                    "pending_finish": len(pending_finish),
                }
            )
    return groups


def multi_account_batch_order(item_ids: list[int]) -> list[int]:
    """Item ids grouped by account, accounts in first-appearance order.

    Reel numbers and frame filenames are both positional, so the prompt builder
    and the frame extractor MUST walk the batch in the same order or reel 4's
    context would point at reel 7's image. Both call this.
    """
    ids = [int(item_id) for item_id in item_ids]
    with get_session() as session:
        account_by_item = {
            item.id: item.account_id
            for item in session.query(DownloadItem).filter(DownloadItem.id.in_(ids)).all()
        }
    grouped: dict[object, list[int]] = {}
    for item_id in ids:
        grouped.setdefault(account_by_item.get(item_id), []).append(item_id)
    return [item_id for group in grouped.values() for item_id in group]


def multi_account_batch_frames(item_ids: list[int]) -> dict:
    """Vision-first frame prep for a cross-account batch (see ``batch_frames``)."""
    return batch_frames(multi_account_batch_order(item_ids))


def build_multi_account_batch_chat_prompt(item_ids: list[int], settings: dict | None = None) -> str:
    """One prompt covering reels from SEVERAL accounts.

    Running six single-account batches re-sends the same ~5k tokens of style
    rules six times. Here the rules are emitted once up front, and an account
    repeats them only when its own niche or style genuinely resolves to
    different rules. Reel headers carry the GLOBAL item id so blocks can be
    routed back across accounts (see ``import_multi_account_batch_draft``).
    """
    settings = settings or {}
    if not item_ids:
        raise DraftRevisionError("Choose at least one item for the batch.")
    # Same walk order as multi_account_batch_frames, so reel N's context and
    # reel N's frame filename always refer to the same clip.
    ids = multi_account_batch_order(item_ids)

    with get_session() as session:
        items = session.query(DownloadItem).filter(DownloadItem.id.in_(ids)).all()
        by_id = {item.id: item for item in items}
        missing = [item_id for item_id in ids if item_id not in by_id]
        if missing:
            raise DraftRevisionError(f"Item(s) not found: {', '.join(map(str, missing))}.")
        ordered = [by_id[item_id] for item_id in ids]
        unassigned = [item.id for item in ordered if item.account_id is None]
        if unassigned:
            raise DraftRevisionError(
                f"Item(s) not assigned to an account: {', '.join(map(str, unassigned))}."
            )

        # Group by account, preserving the order accounts first appear so reel
        # numbering stays aligned with the caller's item order.
        grouped: dict[int, list[DownloadItem]] = {}
        for item in ordered:
            grouped.setdefault(item.account_id, []).append(item)
        accounts = {
            account_id: session.get(Account, account_id) for account_id in grouped
        }
        missing_accounts = [str(a) for a, acc in accounts.items() if acc is None]
        if missing_accounts:
            raise DraftRevisionError(
                f"Account(s) not found: {', '.join(missing_accounts)}."
            )

        rules_by_account = {
            account_id: _account_style_rules(accounts[account_id], settings)
            for account_id in grouped
        }
        shared_rules = next(iter(rules_by_account.values()))
        # Accounts in one niche resolve to near-identical rule blocks: measured
        # across three history accounts, 45 of 46 lines matched and only the
        # audience-echo ban differed (it quotes each account's own audience).
        # Emitting the ~14k-character block once plus a few delta lines is the
        # whole point of batching accounts together, so compare LINE BY LINE
        # rather than requiring exact equality.
        rule_deltas = {
            account_id: [line for line in rules if line not in set(shared_rules)]
            for account_id, rules in rules_by_account.items()
        }
        # A large delta means genuinely different rules (another niche or style),
        # not a voice-derived tweak. Additive deltas would then read as
        # contradictions layered on the shared block, so such an account gets its
        # own full block instead.
        needs_own_rules = {
            account_id
            for account_id, delta in rule_deltas.items()
            if len(delta) > _MAX_RULE_DELTA_LINES
        }
        attachments_expected = any(
            not (item.smart_vision_payload or "").strip() for item in ordered
        )

        lines: list[str] = [
            "You are drafting Instagram Reel titles and captions for SEVERAL NicheFlow "
            "accounts in one pass.",
            "This is for ChatGPT or Claude web.",
            *(
                [
                    "Some reels below ask you to attach and inspect a contact sheet (a grid of "
                "moments from that one clip); the rest "
                    "carry their visual evidence as JSON instead."
                ]
                if attachments_expected
                else [
                    "No images are attached: each reel's visual evidence is provided as JSON "
                    "in its context block."
                ]
            ),
            "",
            f"Accounts in this batch: {len(grouped)}. Total reels: {len(ids)}.",
            "Each account has its own voice block below. Write every reel in the voice of "
            "the account it sits under, and never mix one account's voice into another's.",
            *(
                [f"Caption style: {settings['caption_style']}"]
                if settings.get("caption_style")
                else []
            ),
            *(
                [f"Title style: {settings['title_style']}"]
                if settings.get("title_style")
                else []
            ),
            *(
                [f"Title length: {str(settings['title_length']).title()}"]
                if settings.get("title_length")
                and settings.get("title_style") != TITLE_STYLE_BEATS_V2
                else []
            ),
            f"Clip premise / user direction: {settings.get('clip_premise') or '(none)'}",
            "",
        ]

        lines.extend(
            [
                "Shared style rules (apply to EVERY reel in this batch unless that reel's "
                "account overrides them below):",
                *shared_rules,
                "",
            ]
        )

        lines.extend(
            [
                *_VISUAL_EVIDENCE_GUIDANCE,
                "",
                *_SOURCE_CAPTION_GUIDANCE,
                "",
                *_FACT_CHECK_LINES,
                "",
                *_WEB_RESEARCH_LINES,
                "",
                "Plain-text rules:",
                "- Keep display titles plain text.",
                "- Do not use markdown formatting in section headers or titles.",
                "- Never write em dashes or double hyphens ('--') in titles or captions.",
                "- Do not invent unsupported facts.",
                "- Write THREE title options and exactly ONE 'Shared Caption:' per reel.",
                f"- {_RECOMMENDER_DEFERRAL}",
                "",
                *_BATCH_VARIETY_RULES,
                "",
                "Return one block per reel, one for every reel, in order.",
                "START each reel's block with its own header line that is exactly:",
                "===== REEL <n> (#<id>) ===== <account name>",
                "copying the whole line exactly as shown in that reel's context below. The "
                "(#id) is how the app files each reply to the correct video ACROSS "
                "accounts, so never change, drop, or invent it. The account name after the "
                "bars is what lets a human tell which batch this reply belongs to, so echo "
                "it too.",
                "Do not rename, renumber, merge, or skip any reel, even if two clips look "
                "similar or belong to different accounts.",
                _CONCERN_ESCAPE_HATCH,
                "",
                "Niche fit check (per reel, MANDATORY): compare the clip's actual subject to "
                "the niche of ITS OWN account. Directly under the reel's header line, before "
                "'Title Option 1:', write exactly one line:",
                "Niche check: fits",
                "or",
                "Niche check: OFF-NICHE, <one short reason>",
                "Flag borderline or unclear fits as OFF-NICHE so the user decides. Still "
                "draft all three options either way.",
                "",
                *_field_disambiguation_lines(settings.get("caption_style") or None),
                "Inside each block, use this exact plain-text format:",
                "Niche check: <fits | OFF-NICHE, reason>",
                "Title Option 1:",
                "Title Option 2:",
                "Title Option 3:",
                "Recommended Pick: Title Option N",
                "Why:",
                "Shared Caption:",
                "Selection Notes:",
                "Option 1:",
                "Option 2:",
                "Option 3:",
            ]
        )

        reel_number = 0
        for account_id, account_items in grouped.items():
            account = accounts[account_id]
            lines.extend(
                [
                    "",
                    f"########## ACCOUNT: {account.name} ##########",
                    *_account_voice_delta(account, item_count=len(account_items)),
                ]
            )
            if account_id in needs_own_rules:
                lines.extend(
                    [
                        "",
                        "This account's style rules REPLACE the shared rules above:",
                        *rules_by_account[account_id],
                    ]
                )
            elif rule_deltas[account_id]:
                lines.extend(
                    [
                        "",
                        "Additional rules for this account (on top of the shared rules above):",
                        *rule_deltas[account_id],
                    ]
                )
            lines.append("")
            lines.append("Reel contexts for this account:")
            for item in account_items:
                reel_number += 1
                # The echoed id is the GLOBAL item id here, not the per-account
                # sequence: account-relative numbers repeat across accounts.
                lines.extend(["", *_batch_item_context(reel_number, item, item.id)])

    return "\n".join(lines)


# The "==...=REEL" delimiter form appears mid-line when the chat reply glues
# prose to the first header without a newline ("...verifiable claims.===== REEL
# 1 (#145) ====="). _REEL_HEADER is line-start anchored so prose like "Reel 1 of
# the series" can never become a header; this embedded form is safe to search
# for anywhere in a line because the "==" prefix only comes from the app's own
# delimiter, never from prose.
_EMBEDDED_DELIMITER = re.compile(r"={2,}\s*reel\b", re.IGNORECASE)


def _split_header_line(line: str) -> tuple[str, re.Match[str] | None]:
    """Return (content before the header, header match) for one pasted line.

    Without the embedded check, a header glued to the end of a prose line is
    invisible and that reel's whole block silently lands in the preceding block
    (or is dropped when it is the first reel) — the "Unmatched" failure with a
    correctly formatted reply.
    """
    stripped = line.strip()
    match = _REEL_HEADER.match(stripped)
    if match:
        return "", match
    embedded = _EMBEDDED_DELIMITER.search(stripped)
    if embedded:
        match = _REEL_HEADER.match(stripped[embedded.start() :])
        if match:
            return stripped[: embedded.start()].rstrip(), match
    return "", None


def _split_batch_blocks(text: str) -> list[tuple[int, int | None, str]]:
    """Split a pasted reply into blocks in document order, each as
    ``(reel_number, echoed_id, block_text)``. ``echoed_id`` is the ``(#id)`` from
    the header, or ``None`` when the model dropped it.

    Blocks are deliberately NOT keyed by reel number. Models sometimes renumber
    every block (emitting all of them as "REEL 1") while still echoing distinct,
    correct ids; keying by reel merged those into one block, whose later
    Title/Caption headers then redefined the first block's options -- one item
    silently imported another item's draft and the rest came back unmatched.
    Routing (by echoed id first, reel number only as a fallback) is the caller's
    job, so a block is never dropped before its id has been consulted.
    """
    blocks: list[tuple[int, int | None, list[str]]] = []
    for line in (text or "").splitlines():
        head, match = _split_header_line(line)
        if match:
            # Text glued in front of an embedded delimiter belongs to the block
            # that was open before this header.
            if head and blocks:
                blocks[-1][2].append(head)
            blocks.append((int(match.group(1)), int(match.group(2)) if match.group(2) else None, []))
            continue
        if blocks:
            blocks[-1][2].append(line)
    return [(reel, echoed, "\n".join(lines).strip()) for reel, echoed, lines in blocks]


# Does a routed block actually carry drafts? Models write commentary ABOVE the
# first "===== REEL 1 =====" delimiter and refer to the reels by name there
# ("Reel 1 (#88): verified the Ledger story..."), which _REEL_HEADER accepts as a
# delimiter -- so the note becomes a block that shadows the real one. Emphasis
# chars are tolerated because _strip_header_emphasis unwraps "**Title Option 1:**"
# later; this only decides which block is worth routing.
_DRAFT_SECTION = re.compile(
    r"^[ \t]*[*_#>`]*[ \t]*(?:title|caption)\s*option\s*\d+\s*:",
    re.IGNORECASE | re.MULTILINE,
)


# {batch item ids -> {"#N" -> item_id}} exactly as each account batch prompt
# printed it. The live rank still shifts when an older item of the account is
# deleted, and the reply echoes the numbers the PROMPT carried, so import prefers
# this snapshot. In-memory only: after an app restart import falls back to the
# live rank, which blocks no longer shift (see library.account_sequence_map).
_PROMPT_SEQ_TO_ITEM: dict[frozenset[int], dict[int, int]] = {}


def _batch_seq_to_item(item_ids: list[int]) -> dict[int, int]:
    """Map {account "#id" -> item_id} for this batch, so a reply that echoes a
    "(#143)" routes to the exact video. Batch items share one account, so their
    per-account sequence numbers are unique within the batch."""
    if not item_ids:
        return {}
    snapshot = _PROMPT_SEQ_TO_ITEM.get(frozenset(item_ids))
    if snapshot is not None:
        return dict(snapshot)
    # Lazy import: keep draft_handoff free of library's downloader import chain.
    from nicheflow_studio.services import library

    with get_session() as session:
        seq_by_item = library.account_sequence_map(session)
    return {seq_by_item[item_id]: item_id for item_id in item_ids if item_id in seq_by_item}


# "Prepare batch" copies the PROMPT to the clipboard and Import reads the
# clipboard at click time, so the most common import failure is the user never
# copying the chat reply over it: the prompt's reel blocks carry the same
# "===== REEL n (#id) =====" headers but no Title/Caption sections, so every
# item fails with the generic "No 'Title Option'..." error and no hint at the
# actual mistake. These lines are emitted only by the prompt builders above; a
# reply that follows the return format never contains them.
_PROMPT_PASTE_MARKERS = (
    "Attach and inspect image file:",
    "Visual evidence JSON",
    "Transcript/context excerpt:",
)


def _reject_prompt_paste(text: str) -> None:
    if any(marker in (text or "") for marker in _PROMPT_PASTE_MARKERS):
        raise DraftRevisionError(
            "This is the prepared PROMPT, not the chat reply. The clipboard still "
            "holds what 'Prepare batch' copied. In ChatGPT or Claude, copy the "
            "reply (the blocks starting with 'Title Option 1:'), then import again."
        )


# The account name a reel header carries AFTER its closing "=====" (see
# _batch_item_delimiter). Never a routing key -- routing is by echoed id, then by
# reel number. It matters only as evidence of a WRONG-ROW paste, and it is the
# only such evidence on the single-account path, where account-relative "#id"s
# collide across accounts by design and so can never prove one.
_HEADER_ACCOUNT_LABEL = re.compile(r"={2,}\s*([^=]+?)\s*$")


def _header_account_label(line: str) -> str | None:
    """The account name trailing one reel header, or None when it carries none.

    A header with no name ends in the closing bars, so the group can never reach
    the end of the line and no label is invented.
    """
    match = _HEADER_ACCOUNT_LABEL.search(line.strip())
    if match is None:
        return None
    return match.group(1).strip().strip("*_#`").strip() or None


def _pasted_account_labels(text: str) -> list[str]:
    """Account names echoed by this reply's reel headers, in document order."""
    labels: list[str] = []
    for line in (text or "").splitlines():
        _, match = _split_header_line(line)
        if match is None:
            continue
        label = _header_account_label(line)
        if label:
            labels.append(label)
    return labels


def _normalized_account_name(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def _wrong_batch_message(culprits: list[str], own: list[str]) -> str:
    belongs = ", ".join(culprits) or "another account"
    here = ", ".join(own) or "this"
    return (
        f"This reply is for {belongs}, but Import was pressed on the {here} row. "
        "Nothing was imported. Copy that row's own chat reply and press Import again."
    )


def _requested_account_names(session, requested: list[int]) -> list[str]:
    rows = (
        session.query(Account.name)
        .join(DownloadItem, DownloadItem.account_id == Account.id)
        .filter(DownloadItem.id.in_(requested))
        .distinct()
        .all()
    )
    return sorted({row[0] for row in rows if row[0]})


def _reject_wrong_batch_paste(text: str, requested: list[int], *, global_ids: bool) -> None:
    """Hard-fail a reply pasted onto the wrong prepared row.

    Routing falls back to REEL NUMBER whenever a header's echoed id is not part
    of this batch, so that one garbled id cannot misroute. Applied to a whole
    reply meant for a DIFFERENT row, that same fallback files every block
    positionally onto THIS row's clips: real drafts, all on the wrong account's
    videos, reported as a clean "imported N" and carried through to export and
    scheduling. Nothing downstream can tell them from correct ones, so the paste
    has to die here rather than warn.

    Fires only when NOTHING in the reply points at this batch. A single stray id,
    or an account name the model paraphrased, still imports exactly as before.
    """
    blocks = _split_batch_blocks(text)
    if not blocks:
        return  # no headers at all: the per-item parser reports the real problem
    requested_ids = set(requested)
    with get_session() as session:
        # Strongest evidence, and the only one that needs no cooperation from the
        # model beyond the id it was told never to change. Cross-account replies
        # echo the GLOBAL item id, so an id outside this batch names a real video
        # on some other row.
        if global_ids:
            echoed = [echoed_id for _, echoed_id, _ in blocks if echoed_id is not None]
            if echoed and not requested_ids.intersection(echoed):
                rows = (
                    session.query(DownloadItem.id, Account.name)
                    .outerjoin(Account, Account.id == DownloadItem.account_id)
                    .filter(DownloadItem.id.in_(echoed))
                    .all()
                )
                if rows:
                    culprits = sorted({row[1] for row in rows if row[1]})
                    raise DraftRevisionError(
                        _wrong_batch_message(culprits, _requested_account_names(session, requested))
                    )
        labels = {_normalized_account_name(label) for label in _pasted_account_labels(text)}
        if not labels:
            return
        own = _requested_account_names(session, requested)
        if labels.intersection(_normalized_account_name(name) for name in own):
            return
        # An exact match against a REAL account name, on every header that
        # carries one, with no header naming this row. A paraphrase matches
        # nothing and falls through to the existing routing.
        known = {
            _normalized_account_name(row[0]): row[0]
            for row in session.query(Account.name).all()
            if row[0]
        }
        culprits = sorted({known[label] for label in labels if label in known})
        if culprits:
            raise DraftRevisionError(_wrong_batch_message(culprits, own))


def import_account_batch_draft(text: str, item_ids: list[int]) -> dict:
    """Import a single-account batch reply, routing by account-relative ``#id``."""
    requested = [int(item_id) for item_id in item_ids]
    return _import_batch(text, requested, _batch_seq_to_item(requested), global_ids=False)


def import_multi_account_batch_draft(text: str, item_ids: list[int]) -> dict:
    """Import a CROSS-ACCOUNT batch reply.

    Account-relative "#id"s collide once a batch spans accounts (every account
    numbers its own reels from 1), so the multi-account prompt echoes the GLOBAL
    download-item id instead and routing is an identity map. Everything else --
    reel-number fallback, duplicate handling, per-item error collection -- is
    shared with the single-account path.
    """
    requested = [int(item_id) for item_id in item_ids]
    return _import_batch(
        text, requested, {item_id: item_id for item_id in requested}, global_ids=True
    )


def _rejected_in_batch(item_ids: list[int]) -> set[int]:
    """Which of these reels the user has rejected or blocked since the batch was
    prepared.

    The batch's item list is pinned when the prompt is copied, so a reject made
    afterwards (here or in Processing) leaves the reel in the list and the chat
    reply still carries a block for it. Nothing downstream re-checked review
    state, so the reel got a draft revision, then read as "ready" in the finish
    plan, and was applied, exported, and re-scheduled — silently undoing the
    reject, which had cancelled that clip's publish jobs.
    """
    if not item_ids:
        return set()
    with get_session() as session:
        rows = (
            session.query(DownloadItem.id, DownloadItem.review_state)
            .filter(DownloadItem.id.in_([int(item_id) for item_id in item_ids]))
            .all()
        )
    return {row[0] for row in rows if (row[1] or "") in UNDRAFTABLE_REVIEW_STATES}


def _import_batch(
    text: str, requested: list[int], seq_to_item: dict[int, int], *, global_ids: bool
) -> dict:
    """Import a batch reply. Each block routes to a specific item by the echoed
    ``(#id)`` when it names an item in this batch (most reliable — survives
    reordering, reel-number drift, and duplicate reel numbers), otherwise by REEL
    NUMBER position, which is the only routing key that depends on the order the
    items were selected. An ``(#id)`` that is not part of this batch (e.g. the
    model parroted a frame filename's item id) is ignored and the reel number is
    used instead, so a garbled id never misroutes."""
    _reject_prompt_paste(text)
    _reject_wrong_batch_paste(text, requested, global_ids=global_ids)
    imported: list[int] = []
    failed: list[dict] = []
    routed: dict[int, str] = {}
    order: list[int] = []
    # Rejected reels keep their SLOT in `requested`: the reel-number fallback
    # below routes by position, so dropping one would shift every later reel
    # onto the wrong clip. They are skipped at the save step instead.
    rejected = _rejected_in_batch(requested)
    # Report in reel order; sorted() is stable, so blocks sharing a reel number
    # keep their document order and the first one still wins below.
    for reel, echoed_id, block in sorted(_split_batch_blocks(text), key=lambda b: b[0]):
        item_id = seq_to_item.get(echoed_id) if echoed_id is not None else None
        if item_id is None:
            if reel < 1 or reel > len(requested):
                continue  # reel number outside this batch and no valid id — ignore it
            item_id = requested[reel - 1]
        if item_id not in routed:
            routed[item_id] = block
            order.append(item_id)
        elif not _DRAFT_SECTION.search(routed[item_id]) and _DRAFT_SECTION.search(block):
            # Duplicate target: the first block still wins between two real
            # drafts, but a block with no Title/Caption sections at all is a
            # stray (a preamble note the model addressed to "Reel 1 (#88):")
            # and must not shadow the block that actually carries the draft.
            routed[item_id] = block
    for item_id in order:
        if item_id in rejected:
            continue
        try:
            import_pasted_draft(item_id, routed[item_id])
            imported.append(item_id)
        except DraftRevisionError as exc:
            failed.append({"item_id": item_id, "error": str(exc)})
    matched = set(imported) | {row["item_id"] for row in failed} | rejected
    return {
        "imported": imported,
        "failed": failed,
        # Reported separately from "unmatched" (a reply that lost a block) so the
        # count reads as a deliberate exclusion rather than a parse failure.
        "skipped": [
            {"item_id": item_id, "reason": "rejected since this batch was prepared"}
            for item_id in requested
            if item_id in rejected
        ],
        "unmatched": [item_id for item_id in requested if item_id not in matched],
    }


def batch_frames(item_ids: list[int]) -> dict:
    """Prepare each reel's visual grounding for the chat handoff.

    Vision-first: run the Groq vision pass over every item that has no visual
    evidence yet, and attach an image ONLY for the items it could not cover.
    ``described`` lets the UI say how many reels are vision-backed.

    The attachment is a contact sheet rather than the single middle still it used
    to be. A still shows one moment, which is how a draft ends up describing a
    man at a podium while missing that the cartoon characters are seated in the
    audience. Measured over six library clips, a 6-tile sheet averages ~930
    image tokens against ~971 for one full-height still, because cropping the
    letterbox and the burned-in title banner reclaims more than the extra tiles
    cost. More evidence, slightly cheaper.
    """
    ids = [int(item_id) for item_id in item_ids]
    if not ids:
        raise DraftRevisionError("Choose at least one item for the batch.")
    # Before anything else, because the PROMPT builder reads the hook straight
    # from the database and the UI calls this step first. Doing it here rather
    # than at distribute time means only reels actually being drafted pay for it,
    # and the footage is guaranteed on disk, which it is not when a clip is
    # freshly assigned.
    _ensure_on_screen_hooks(ids)
    _ensure_transcripts(ids)
    vision = draft_generation.ensure_batch_vision_payloads(ids)
    described = set(vision["described"])

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = processed_dir() / "chatgpt-batches" / f"batch-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    for index, item_id in enumerate(ids, start=1):
        if item_id in described:
            continue  # vision JSON carries this reel; no attachment needed
        target = folder / f"reel_{index}_item{item_id}.jpg"
        try:
            _write_contact_sheet(item_id, target)
        except (ExportError, DraftRevisionError) as exc:
            raise DraftRevisionError(f"Reel {index} (item {item_id}): {exc}") from exc
        frames.append({"item_id": item_id, "path": str(target)})
    return {
        "folder": str(folder),
        "frames": frames,
        "described": sorted(described),
        "skipped": vision["skipped"],
    }


def _ensure_on_screen_hooks(item_ids: list[int]) -> int:
    """OCR the source's burned-in hook for any reel that has none yet.

    Reading it is the difference between two shipping rules firing and sitting
    dead: the evidence block's "match the angle, never the wording" and
    ``title_lab``'s hook-is-not-evidence rule both need a hook to point at, and the field was
    empty on 161 of 185 reels on the live account.

    Best-effort by design. A reel whose banner will not read still drafts from
    its caption and contact sheet, so no failure here may block a batch. Reads
    below the confidence floor are dropped rather than stored: the prompt tells
    the model to match whatever angle it is handed, so a garbled hook would steer
    every option on that reel, and none is better than wrong.
    """
    written = 0
    for item_id in item_ids:
        try:
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None:
                    continue
                video_id = (item.video_id or "").strip()
                path = Path(item.file_path or "")
                if not video_id or not item.file_path:
                    continue
                existing = (
                    session.query(ScrapeCandidate.on_screen_title)
                    .filter(ScrapeCandidate.video_id == video_id)
                    .limit(1)
                    .scalar()
                )
                if (existing or "").strip():
                    continue
            if not path.exists():
                continue
            hook, score = on_screen_hook.read_hook(path)
            if not hook or score < on_screen_hook.MIN_HOOK_SCORE:
                continue
            with get_session() as session:
                rows = (
                    session.query(ScrapeCandidate)
                    .filter(ScrapeCandidate.video_id == video_id)
                    .all()
                )
                for row in rows:
                    row.on_screen_title = hook
                session.commit()
                written += len(rows)
        except Exception:  # noqa: BLE001 - a hook is a bonus, never a blocker
            logger.exception("Reading the on-screen hook for item %s failed.", item_id)
    return written


# What faster-whisper says when vad_filter finds no speech. A music-only
# repost is the common case here, not an error, so it is skipped quietly
# instead of filling the log with stack traces on every batch.
_NO_SPEECH_MARKER = "no speech was detected"

# Minimum words before a transcript is worth storing.
#
# Whisper's VAD returns nothing at all on most music-only reposts, which is the
# clean case. The dangerous case is the one in between: a reel scored with music
# under which it hallucinates a single short fragment. Item 1959 (1970s Iran,
# music only) produced "I passed along the state" -- five words of pure noise
# that would then sit in the prompt under "Transcript/context excerpt:" as if it
# were evidence, steering all three titles. None beats wrong, the same rule
# _ensure_on_screen_hooks applies to a low-confidence OCR read.
#
# Measured over 12 library reels: the two hallucinations were 5 words each; real
# speech ran 16, 30, 60, 60, 72, 100 and 169 words; four reels returned nothing.
# Ten sits in the gap with margin on both sides. A genuinely terse clip loses its
# transcript, which costs little -- six words of dialogue ground a draft barely
# better than silence does.
_MIN_TRANSCRIPT_WORDS = 10


def _ensure_transcripts(item_ids: list[int]) -> int:
    """Transcribe the audio of any reel that has none yet.

    Every title in this library was drafted with ``transcript_text`` empty,
    leaving the reposter's scraped caption as the ONLY statement of who or what
    is in a clip -- and a reposter's caption is a claim, not evidence. Two live
    cases came out of that one gap: item 1959, whose caption described the Qajar
    dynasty (ended 1925) over footage of 1970s Iran, and item 1948, whose
    caption credited Snoop Dogg for a clip he never appears in. The second was
    CORRECT (his narration IS the audio track) and from the draft alone looked
    exactly as wrong as the first. Reading the audio is what tells them apart.

    Best-effort on the same terms as ``_ensure_on_screen_hooks``: a reel with no
    audio stream, no speech, or a whisper failure still drafts from its caption
    and contact sheet, so nothing here may block a batch.
    """
    written = 0
    for item_id in item_ids:
        try:
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None or not item.file_path:
                    continue
                if (item.transcript_text or "").strip():
                    continue  # already transcribed, or hand-written by the user
                path = Path(item.file_path)
            if not path.exists() or not video.has_audio_stream(path):
                continue
            # Subprocess, not in-process: whisper pins a core for the whole
            # decode and a fault in the native runtime would take the app with
            # it. The module already ships this entry point for the same reason.
            draft = transcription.generate_transcript_draft_in_subprocess(path)
            text = (draft.transcript_text or "").strip()
            if len(text.split()) < _MIN_TRANSCRIPT_WORDS:
                continue
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None:
                    continue
                # ONLY the transcript. TranscriptDraft also carries title and
                # caption drafts; writing those here would silently clobber a
                # draft the user has already reviewed and applied.
                item.transcript_text = text
                session.commit()
                written += 1
        except Exception as exc:  # noqa: BLE001 - a transcript is a bonus, never a blocker
            if _NO_SPEECH_MARKER in str(exc).casefold():
                continue
            logger.exception("Transcribing item %s failed.", item_id)
    return written


def _write_contact_sheet(item_id: int, target: Path) -> None:
    """Render one reel's contact sheet, falling back to the single still.

    Sheet building runs ffmpeg several times (scene detection, an overlay probe,
    one pass per tile). Any of those can fail on a truncated download, and a
    batch that refuses to prepare is worse than one reel grounded on a single
    frame, so failure degrades to the old behaviour instead of raising.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise DraftRevisionError(f"No download item with id {item_id}.")
        source_path = Path(item.file_path) if item.file_path else None

    if source_path is not None and source_path.exists():
        try:
            video.build_contact_sheet(source_path, target)
            return
        except Exception:  # noqa: BLE001 - any ffmpeg/probe failure falls back
            logger.warning("Contact sheet failed for item %s; using a single frame", item_id)

    shutil.copyfile(export_svc.crop_preview_frame(item_id), target)


def import_pasted_draft(item_id: int, text: str) -> DraftRevisionDTO:
    _reject_prompt_paste(text)
    parsed = parse_pasted_draft(text)
    if not any(value.strip() for value in [*parsed.title_options, *parsed.caption_options]):
        raise DraftRevisionError(
            "No 'Title Option' or 'Shared Caption' sections found in the clipboard text."
        )
    # A half-parsed reply is worse than a rejected one: with (say) captions 2-3
    # parsed but their title headers unrecognized, save_revision would drop the
    # empty titles and store a 1-title/3-caption revision — the UI then shows a
    # single option card and Apply's shared title/caption index pairing breaks.
    # Fail loudly and name the exact headers that didn't parse instead.
    paired = list(zip(parsed.title_options, parsed.caption_options))
    missing_titles = [str(i + 1) for i, (t, c) in enumerate(paired) if c.strip() and not t.strip()]
    missing_captions = [
        str(i + 1) for i, (t, c) in enumerate(paired) if t.strip() and not c.strip()
    ]
    if missing_titles or missing_captions:
        problems = []
        if missing_titles:
            problems.append(f"Title Option {'/'.join(missing_titles)}")
        if missing_captions:
            # Every option shares one caption, so a gap here means the whole
            # 'Shared Caption:' header failed to parse, not option N's own.
            problems.append(f"the caption for Option {'/'.join(missing_captions)}")
        raise DraftRevisionError(
            f"{' and '.join(problems)} did not parse while the paired sections did — those "
            "header lines deviated from the plain 'Title Option N:' / 'Shared Caption:' "
            "format. Fix the header lines in the reply (or ask the model to re-emit "
            "plain-text headers) and import again."
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
            account.niche_label if account else None,
            item.smart_vision_payload if item else None,
        )
        # The scraped post's own caption is another reposter's claim, not
        # evidence we observed — kept separate so it flags instead of clears.
        asserted_text = draft_guard.build_signals_text(
            item.source_description if item else None,
        )
        # The account's saved Title Length, the same resolution the prompt used.
        _, _, title_length = _resolved_styles(account, {})
    guarded = draft_guard.guard_options(
        title_options=parsed.title_options,
        signals_text=signals_text,
        asserted_signals_text=asserted_text,
        option_notes=parsed.option_notes,
        recommended_index=(
            parsed.recommended_title_index - 1 if parsed.recommended_title_index else None
        ),
        recommendation_reason=parsed.reason,
        max_title_chars=draft_guard.title_char_limit(title_length),
    )
    # One metadata-referential title among three is a bad option the guard has
    # already reddened and moved the pick off. ALL of them means the model
    # declined to draft this reel and wrote its reasoning into the title slots
    # -- usually because the scraped caption contradicts the footage. Saving
    # that produces a revision whose every option is unpostable, and Apply then
    # burns one onto the video (item 1959, "The caption attached to this clip
    # is about a dynasty gone a century ago"). Refuse the import and say what
    # to do instead.
    filled = [i for i, title in enumerate(parsed.title_options) if title.strip()]
    if filled and all(i in guarded.meta_terms for i in filled):
        phrases = sorted({term for i in filled for term in guarded.meta_terms[i]})
        raise DraftRevisionError(
            "Every title option refers to the clip's metadata rather than what is on "
            f"screen ({', '.join(phrases)}). A title is burned into the video, so the "
            "viewer cannot see the source caption it is talking about. This is what a "
            "reply looks like when the model decided the reel is undraftable, usually "
            "because the scraped caption contradicts the footage. Nothing was saved. "
            "Either reject this reel, or ask for titles written from the visible scene "
            "and import again."
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
