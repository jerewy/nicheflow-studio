"""Deterministic grounding guard for generated draft titles.

Every intake path can deliver a title that states a concrete claim ("heavy",
"rarest", "1950") that no clip signal supports — the exact titles that draw
"that's not true" comments. The Groq prompt asks the model to self-rate
options (green/yellow/red) and to cite the supporting phrase per claim, but
self-rating catches only what the model knows it claimed, and clipboard
imports from chat assistants arrive with no rating at all.

This module is the shared, model-free check: a small lexicon of claim words
(plus 4-digit years) matched against the combined clip signals (transcript,
source caption, niche, visual evidence). Support stems are deliberately
broader than the claim word so a paraphrased source still counts ("heavy" is
supported by "weighed 30kg"). A claim with no support downgrades the option
to red and blocks it from being the recommended pick — the option itself is
kept so the user can still choose it knowingly.

Pure functions only: no DB, no network, no LLM.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Weight/money paraphrases need a number+unit shape, not a bare stem: "ton"
# alone would match Boston/cotton, "lb" would match albums.
_WEIGHT_EVIDENCE_RE = re.compile(r"\d\s*(kg|kilo|kilogram|lb|lbs|pound|ton|tonne)")
_MONEY_EVIDENCE_RE = re.compile(r"[$€£]\s*\d|\d\s*(dollar|euro|pound|cost|price)")

# Claim word found in a TITLE -> evidence that counts as support in the
# SIGNALS text (plain substrings, or regexes for shapes a substring can't
# express safely). Precision over recall: only words that are nearly always
# concrete factual claims, so meme/interpretive titles never get flagged.
_CLAIM_SUPPORT: dict[str, tuple[str | re.Pattern[str], ...]] = {
    # Weight.
    "heavy": ("heav", "weigh", _WEIGHT_EVIDENCE_RE),
    "heaviest": ("heaviest", "weigh", _WEIGHT_EVIDENCE_RE),
    # Rarity / uniqueness (the prompt's RED list; if a model writes these
    # anyway, the guard is the backstop).
    "rare": ("rare",),
    "rarest": ("rarest",),
    "never before": ("never before", "first time"),
    "first": ("first", "debut", "earliest"),
    "last": ("last", "final"),
    "only known": ("only known", "only surviving", "sole"),
    # Timeline finality — same family as "last": "his final rehearsal" or
    # "never again" reads as a verified biography fact, so the source itself
    # must say it (the MJ "last rehearsal vs 36 hours" case).
    "final": ("final", "last"),
    "never again": ("never again", "never"),
    "only time": ("only time", "only once", "only"),
    # Money.
    "expensive": ("expens", "cost", "price", "worth", _MONEY_EVIDENCE_RE),
    "priceless": ("priceless", "worth", "value"),
    # Measurable superlatives.
    "oldest": ("oldest",),
    "youngest": ("youngest",),
    "biggest": ("biggest", "largest"),
    "largest": ("largest", "biggest"),
    "smallest": ("smallest", "tiniest"),
    "tallest": ("tallest", "height"),
    "shortest": ("shortest", "height"),
    "longest": ("longest",),
    "fastest": ("fastest", "speed"),
    "slowest": ("slowest",),
    "strongest": ("strongest",),
    "richest": ("richest", "wealth"),
    "deadliest": ("deadliest",),
    # Legality / secrecy.
    "banned": ("banned", "prohibit", "illegal", "outlaw"),
    "illegal": ("illegal", "banned", "outlaw", "law"),
    "forbidden": ("forbidden", "banned", "prohibit"),
    "secret": ("secret", "classified", "hidden"),
    # Venue / place. A named venue is one of the easiest things for a viewer
    # to fact-check and one of the most common corrections we get: a real
    # title read "On the palace lawn" for footage shot on the Government
    # House lawn in Auckland, and the top comment was the correction. Only
    # venue-TYPE nouns are listed (open-ended city and country names cannot be
    # a lexicon); each is supported by its own stem, so a title may say
    # "palace" whenever any signal does.
    "palace": ("palace",),
    "castle": ("castle",),
    "cathedral": ("cathedral", "church", "basilica"),
    "stadium": ("stadium", "arena"),
    "arena": ("arena", "stadium"),
    "parliament": ("parliament", "congress", "senate"),
    "embassy": ("embassy", "consulate"),
    "prison": ("prison", "jail", "penitentiary"),
    "museum": ("museum", "gallery"),
    "courtroom": ("courtroom", "courthouse", "court", "trial"),
    # Setting and grouping claims. These are the one category the guard could
    # not see: a claim read off the contact sheet has no text to check against,
    # so anything outside this lexicon defaulted to green. A real title read
    # "What made a room full of psychiatrists laugh at that question?" over
    # footage of two people interviewed SEPARATELY (one at a convention, one on
    # a city street) and cleared green, while the accurate option naming the
    # convention came back yellow -- the guard was rewarding vagueness.
    #
    # Phrases, not the bare noun: "room" is polysemous ("room to grieve", "the
    # Macarena took over every room") and a bare entry mis-flagged 30 of the 40
    # historical uses. These patterns flag 1.7% of posted titles, and most of
    # those are correct claims, which is why they land on YELLOW for review
    # rather than red -- same reasoning as asserted_terms above.
    # Only GROUP claims are listed. "on stage" and a bare "audience" were tried
    # and dropped: a stage or a crowd is usually visible rather than described,
    # so they flagged 17 of 23 legitimate performance titles. A group claim is
    # different -- it asserts that several people were together in one place,
    # which is precisely what gets invented when the footage is a set of
    # separate interviews and only the contact sheet is doing the talking.
    "room full of": ("room", "crowd", "audience", "classroom", "hall", "auditorium"),
    "roomful of": ("room", "crowd", "audience", "classroom", "hall", "auditorium"),
    "whole room": ("room", "crowd", "audience", "hall", "auditorium", "everyone"),
    "entire room": ("room", "crowd", "audience", "hall", "auditorium", "everyone"),
    "crowd of": ("crowd", "audience", "room", "spectators", "gathering"),
}
# Words whose bare form is too broad to detect on, same reasoning as the
# "room full of" phrases above: the entry means one sense and the word carries
# others.
#
# "first" is the lexicon's most frequent trigger by a factor of four (213 of
# 7,073 titles in the account's own posted + drafted set), and 42 of those 213
# are the ADVERBIAL sense — "who leaves first", "refuses to blink first",
# "remembered as a mother first" — which asserts no precedence at all. The
# signals almost never contain the literal word, so each one came back
# unsupported and `_enforce_recommendation` moved the pick off it: a real batch
# demoted "the audience gave him twelve straight minutes of applause first"
# (Chaplin's 1972 Oscar ovation, independently verifiable) onto an option that
# named nobody.
#
# The precedence sense always modifies something — "first time", "first U.S.
# concert", "first 900 ever" — so require a following word and exclude the set
# phrases that read adverbially despite having one.
_CLAIM_DETECT_OVERRIDES: dict[str, re.Pattern[str]] = {
    "first": re.compile(r"(?<!\bat )\bfirst\s+(?!of\s+all\b|off\b)\w"),
}
_CLAIM_WORD_RES: dict[str, re.Pattern[str]] = {
    word: _CLAIM_DETECT_OVERRIDES.get(word, re.compile(rf"\b{re.escape(word)}\b"))
    for word in _CLAIM_SUPPORT
}
# Years (1600-2099, optional decade 's'): a title saying "1950s" is a date
# claim; the digits must appear somewhere in the signals.
_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2})s?\b")
# Precise durations ("36 hours before his death"): the number+unit pair must
# appear in the signals — a different number than the source's is the classic
# silent corruption of a timeline claim.
_DURATION_RE = re.compile(
    r"\b(\d{1,4})\s*(second|minute|hour|day|week|month|year|decade)s?\b"
)

# Titles that talk ABOUT the clip's metadata instead of to the viewer.
#
# The title is burned into the exported video (top_band/overlay), so the viewer
# reads it with no access to the scraped caption, the source post, or our own
# review notes. A line like "The caption attached to this clip is about a
# dynasty gone a century ago" points at something outside the frame: true,
# unusable, and unreadable as a hook. It appears when a chat model finds the
# source caption contradicting the footage and writes that FINDING into the
# title slot rather than drafting from the visible scene or declining outright
# -- an operator-facing sentence in a viewer-facing field. The claim lexicon
# above cannot catch it, because such a sentence asserts nothing checkable
# about the subject and so comes back green.
#
# Phrases, not bare nouns, on the same precision-over-recall rule as the group
# claims: "clip", "footage" and "source" are ordinary title vocabulary ("this
# clip was banned for 40 years"), so only the metadata-referring and
# refusal-shaped combinations are listed. Apostrophes survive _normalize_text,
# hence the ['\u2019]? alternations.
_META_REFERENCE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "caption attached",
        re.compile(
            r"\bcaption (?:attached|says|said|claims|claimed|describes|described|calls|"
            r"called|is about|was about|is wrong|is false|does not match|doesn['\u2019]?t match)\b"
        ),
    ),
    (
        "attached to this clip",
        re.compile(r"\battached to th(?:is|e) (?:clip|video|footage|post|reel)\b"),
    ),
    ("what's on screen", re.compile(r"\bwhat(?:['\u2019]?s| is) (?:actually )?on screen\b")),
    ("on screen is not", re.compile(r"\bon screen is (?:not|nothing)\b")),
    ("mislabeled", re.compile(r"\bmis(?:labell?ed|attributed|captioned|dated)\b")),
    ("unverified", re.compile(r"\bun(?:verified|confirmed)\b")),
    (
        "cannot be verified",
        re.compile(
            r"\b(?:cannot|can ?not|could ?not|couldn['\u2019]?t|can['\u2019]?t) be "
            r"(?:verified|confirmed|identified)\b"
        ),
    ),
    (
        "the source caption",
        re.compile(r"\bthe (?:source|original) (?:caption|post|upload|uploader|description)\b"),
    ),
    ("the uploader", re.compile(r"\bthe (?:uploader|reposter|original poster)\b")),
    (
        "until the source is confirmed",
        re.compile(r"\buntil the (?:real |actual )?(?:source|origin|date)\b"),
    ),
)

# claim_support values that mean "no citation given" rather than a quote.
_NO_CITATION_VALUES = {"", "none", "n/a", "na", "none needed", "not needed", "no claim"}

_TIER_RANK = {"green": 0, "yellow": 1, "red": 2}


def _normalize_text(text: str | None) -> str:
    """Casefold + collapse whitespace + dashes-to-spaces, so 'never-before-seen'
    matches 'never before' and quoting differences don't break substring checks."""
    text = (text or "").casefold()
    text = re.sub(r"[—–\-_]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def build_signals_text(*parts: object) -> str:
    """Join clip signals (strings, dicts, None) into one searchable text blob."""
    chunks: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (dict, list)):
            text = json.dumps(part, ensure_ascii=False)
        else:
            text = str(part).strip()
        if text and text.casefold() not in {"(none)", "none", "null", "{}"}:
            chunks.append(text)
    return "\n".join(chunks)


@dataclass(frozen=True)
class TitleClaimReport:
    claim_terms: list[str]
    unsupported_terms: list[str]
    asserted_terms: list[str] = field(default_factory=list)


def check_title_claims(
    title: str,
    signals_text: str,
    citation: str | None = None,
    asserted_text: str | None = None,
) -> TitleClaimReport:
    """Find concrete claim terms in ``title`` and which lack signal support.

    A citation (the model's quoted supporting phrase) clears the whole option,
    but only when the quote actually appears in the signals — an invented
    quote changes nothing.

    ``asserted_text`` holds signals we did not observe ourselves — chiefly the
    scraped post's own caption, written by another repost account. Those are
    second-hand claims, not evidence: reposters routinely embellish dates,
    records and identities, so treating their caption as proof launders their
    mistakes into our titles as confident fact. Terms backed ONLY there come
    back as ``asserted_terms`` (flag for review) rather than either silently
    supported or hard-red.
    """
    normalized_title = _normalize_text(title)
    normalized_signals = _normalize_text(signals_text)
    normalized_asserted = _normalize_text(asserted_text)

    claim_terms: list[str] = [
        word for word, pattern in _CLAIM_WORD_RES.items() if pattern.search(normalized_title)
    ]
    claim_terms.extend(match.group(1) for match in _YEAR_RE.finditer(normalized_title))
    claim_terms.extend(
        f"{match.group(1)} {match.group(2)}"
        for match in _DURATION_RE.finditer(normalized_title)
    )
    if not claim_terms:
        return TitleClaimReport(claim_terms=[], unsupported_terms=[])

    # A citation clears the option only when it quotes something we observed.
    # Quoting the reposter's caption is exactly the laundering case above, so
    # it downgrades to "asserted" instead of clearing.
    normalized_citation = _normalize_text((citation or "").strip("\"'“”‘’"))
    cited = normalized_citation not in _NO_CITATION_VALUES
    if cited and normalized_citation in normalized_signals:
        return TitleClaimReport(claim_terms=claim_terms, unsupported_terms=[])
    if cited and normalized_asserted and normalized_citation in normalized_asserted:
        return TitleClaimReport(
            claim_terms=claim_terms, unsupported_terms=[], asserted_terms=claim_terms
        )

    unsupported: list[str] = []
    asserted: list[str] = []
    for term in claim_terms:
        if _term_supported(term, normalized_signals):
            continue
        if normalized_asserted and _term_supported(term, normalized_asserted):
            asserted.append(term)
        else:
            unsupported.append(term)
    return TitleClaimReport(
        claim_terms=claim_terms, unsupported_terms=unsupported, asserted_terms=asserted
    )


def find_meta_references(title: str) -> list[str]:
    """Phrases in ``title`` that address the operator instead of the viewer.

    Returns the matched labels (possibly several), empty when the title reads
    as a normal hook. See ``_META_REFERENCE_RES`` for why this is separate from
    the claim check: a metadata reference is not a false claim, it is a true
    sentence aimed at the wrong reader.
    """
    normalized = _normalize_text(title)
    return [label for label, pattern in _META_REFERENCE_RES if pattern.search(normalized)]


_UNIT_WORDS = (
    "one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS_WORDS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_SPELLED_NUMBER_RE = re.compile(
    rf"\b(?:({'|'.join(_TENS_WORDS)})(?: ({'|'.join(_UNIT_WORDS[:9])}))?|"
    rf"({'|'.join(_UNIT_WORDS)}))\b"
)


def _spelled_numbers_as_digits(text: str) -> str:
    """'sixty nine years' -> '69 years' (text is already dash-normalized)."""

    def to_digits(match: re.Match[str]) -> str:
        tens, unit, single = match.groups()
        if single:
            return str(_UNIT_WORDS.index(single) + 1)
        return str((_TENS_WORDS.index(tens) + 2) * 10 + (_UNIT_WORDS.index(unit) + 1 if unit else 0))

    return _SPELLED_NUMBER_RE.sub(to_digits, text)


def _duration_supported(term: str, normalized_signals: str) -> bool:
    """A duration is backed by the same number+unit, spelled or in digits, or for
    years by two signal years that far apart: sources write "Sixty-nine years
    later" or give 1953 and 2022, and a title saying "69 years apart" is the
    same fact, not an invented one."""
    if term in _spelled_numbers_as_digits(normalized_signals):
        return True
    number, unit = term.split(" ")
    if unit != "year":
        return False
    years = {int(year) for year in _YEAR_RE.findall(normalized_signals)}
    return any(year + int(number) in years for year in years)


def _term_supported(term: str, normalized_signals: str) -> bool:
    if term not in _CLAIM_SUPPORT:  # a year or duration: the digits are the evidence
        if term in normalized_signals:
            return True
        return bool(_DURATION_RE.fullmatch(term)) and _duration_supported(term, normalized_signals)
    for stem in _CLAIM_SUPPORT[term]:
        if isinstance(stem, re.Pattern):
            if stem.search(normalized_signals):
                return True
        elif stem in normalized_signals:
            return True
    return False


@dataclass(frozen=True)
class GuardedDraftOptions:
    option_tiers: list[str]
    option_notes: list[str] | None
    recommended_index: int | None  # 0-based; title and caption move together
    recommendation_reason: str | None
    flagged_terms: dict[int, list[str]] = field(default_factory=dict)
    recommendation_shifted: bool = False
    # Option index -> matched metadata phrases. Kept apart from flagged_terms
    # because the caller treats them differently: an unsupported claim is a
    # title to double-check, a metadata reference is a title that cannot ship.
    meta_terms: dict[int, list[str]] = field(default_factory=dict)


# Hard per-option ceiling behind the "Short" Title Length setting. The prompt
# asks for it too, but August 2026 showed a prompt length rule alone gets
# ignored (the "short" option came back at a median ~110 characters), so the
# guard enforces it after the fact.
#
# 60, not 70, because 60 is where the measured band actually is: across 1,626
# posted reels (2026-09-12) titles of 60 characters or fewer hit 100k+ views
# 15-17% of the time on beneathhistory, pastmomentsdaily and theoddhistoryclub,
# against 3-13% for 61-100. A 70-char cap lands inside that weaker band, which
# is exactly what the first Short batch did (median 65).
SHORT_TITLE_MAX_CHARS = 60


def title_char_limit(title_length: str | None) -> int | None:
    """The hard character ceiling for a Title Length setting; only "short" has one."""
    return SHORT_TITLE_MAX_CHARS if (title_length or "").strip().lower() == "short" else None


def _normalize_tier(value: object) -> str:
    lowered = str(value or "").casefold()
    return next((tier for tier in _TIER_RANK if tier in lowered), "yellow")


def _format_terms(terms: list[str]) -> str:
    return ", ".join(f"'{term}'" for term in terms)


def guard_options(
    *,
    title_options: list[str],
    signals_text: str,
    asserted_signals_text: str | None = None,
    option_tiers: list[str] | None = None,
    option_notes: list[str] | None = None,
    claim_supports: list[str] | None = None,
    recommended_index: int | None = None,
    recommendation_reason: str | None = None,
    max_title_chars: int | None = None,
) -> GuardedDraftOptions:
    """Run the grounding check over every title and enforce the recommendation.

    ``max_title_chars`` (see :func:`title_char_limit`) reddens any option longer
    than the account's length setting allows, so the pick moves to one that fits.

    With ``option_tiers`` provided (Groq path) the guard only downgrades:
    an option whose claim has no support becomes red, clean options keep the
    model's tier. Without tiers (clipboard path) the guard derives them: green
    when a title makes no checkable claim, yellow when its claims are
    supported, red when one is not. If the recommended pick lands on a red
    option, the recommendation moves to the strongest clean option instead —
    the flagged option is kept (not rewritten) so the user can still choose it.
    """
    flagged: dict[int, list[str]] = {}
    meta_terms: dict[int, list[str]] = {}
    too_long: dict[int, int] = {}
    tiers: list[str] = []
    notes = [
        option_notes[i] if option_notes and i < len(option_notes) else ""
        for i in range(len(title_options))
    ]
    for i, title in enumerate(title_options):
        citation = claim_supports[i] if claim_supports and i < len(claim_supports) else None
        report = check_title_claims(title, signals_text, citation, asserted_signals_text)
        if option_tiers and i < len(option_tiers):
            tier = _normalize_tier(option_tiers[i])
        else:
            tier = "green" if not report.claim_terms else "yellow"
        if report.unsupported_terms:
            flagged[i] = report.unsupported_terms
            tier = "red"
            warning = (
                f"Grounding check: {_format_terms(report.unsupported_terms)} not backed by "
                "the transcript or visual evidence; verify before posting."
            )
            notes[i] = f"{notes[i]} [{warning}]".strip() if notes[i] else warning
        elif report.asserted_terms:
            # Second-hand only: the reposter said it, we did not see it. Never
            # green (it is a checkable claim we cannot check) but not red
            # either, since it is usually right — the user decides.
            tier = "yellow" if _TIER_RANK[tier] < _TIER_RANK["yellow"] else tier
            warning = (
                f"Unverified: {_format_terms(report.asserted_terms)} comes only from the "
                "original poster's caption, not the clip itself; check it before posting."
            )
            notes[i] = f"{notes[i]} [{warning}]".strip() if notes[i] else warning
        # Independent of the claim check and unconditional: however well
        # grounded, a title that refers to the clip's metadata is not a title.
        meta = find_meta_references(title)
        if meta:
            meta_terms[i] = meta
            tier = "red"
            warning = (
                f"Not a viewer-facing title: {_format_terms(meta)} refers to the clip's "
                "metadata or to our own review, which the viewer never sees on screen. "
                "Rewrite it as a hook about what is visible."
            )
            notes[i] = f"{notes[i]} [{warning}]".strip() if notes[i] else warning
        if max_title_chars is not None and len(title) > max_title_chars:
            too_long[i] = len(title)
            tier = "red"
            warning = (
                f"Too long for this account's Short title setting ({len(title)} > "
                f"{max_title_chars} characters); pick or write a shorter option."
            )
            notes[i] = f"{notes[i]} [{warning}]".strip() if notes[i] else warning
        tiers.append(tier)

    new_index, reason, shifted = _enforce_recommendation(
        tiers=tiers,
        flagged=flagged,
        recommended_index=recommended_index,
        recommendation_reason=recommendation_reason,
        meta_terms=meta_terms,
        too_long=too_long,
    )
    return GuardedDraftOptions(
        option_tiers=tiers,
        option_notes=notes if any(notes) else None,
        recommended_index=new_index,
        recommendation_reason=reason,
        flagged_terms=flagged,
        recommendation_shifted=shifted,
        meta_terms=meta_terms,
    )


def _enforce_recommendation(
    *,
    tiers: list[str],
    flagged: dict[int, list[str]],
    recommended_index: int | None,
    recommendation_reason: str | None,
    meta_terms: dict[int, list[str]] | None = None,
    too_long: dict[int, int] | None = None,
) -> tuple[int | None, str | None, bool]:
    if recommended_index is None or not (0 <= recommended_index < len(tiers)):
        return recommended_index, recommendation_reason, False
    if tiers[recommended_index] != "red":
        return recommended_index, recommendation_reason, False

    terms = flagged.get(recommended_index)
    meta = (meta_terms or {}).get(recommended_index)
    length = (too_long or {}).get(recommended_index)
    if terms:
        claim_text = f"{_format_terms(terms)} has no support in the clip signals"
    elif meta:
        claim_text = f"its title refers to the clip's metadata ({_format_terms(meta)})"
    elif length:
        claim_text = f"its title is {length} characters, over the Short title limit"
    else:
        claim_text = "its title makes an unverifiable claim"
    candidates = sorted(
        (i for i, tier in enumerate(tiers) if tier != "red"),
        key=lambda i: (_TIER_RANK[tiers[i]], i),
    )
    if not candidates:
        warning = f"Warning: {claim_text}; verify before posting."
        reason = f"{recommendation_reason} [{warning}]" if recommendation_reason else warning
        return recommended_index, reason, False

    shift_note = f"Auto-moved from Option {recommended_index + 1}: {claim_text}."
    reason = f"{recommendation_reason} [{shift_note}]" if recommendation_reason else shift_note
    return candidates[0], reason, True
