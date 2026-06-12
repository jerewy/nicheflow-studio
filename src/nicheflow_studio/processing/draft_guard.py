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
}
_CLAIM_WORD_RES: dict[str, re.Pattern[str]] = {
    word: re.compile(rf"\b{re.escape(word)}\b") for word in _CLAIM_SUPPORT
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


def check_title_claims(
    title: str, signals_text: str, citation: str | None = None
) -> TitleClaimReport:
    """Find concrete claim terms in ``title`` and which lack signal support.

    A citation (the model's quoted supporting phrase) clears the whole option,
    but only when the quote actually appears in the signals — an invented
    quote changes nothing.
    """
    normalized_title = _normalize_text(title)
    normalized_signals = _normalize_text(signals_text)

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

    normalized_citation = _normalize_text((citation or "").strip("\"'“”‘’"))
    if normalized_citation not in _NO_CITATION_VALUES and normalized_citation in normalized_signals:
        return TitleClaimReport(claim_terms=claim_terms, unsupported_terms=[])

    unsupported = [
        term
        for term in claim_terms
        if not _term_supported(term, normalized_signals)
    ]
    return TitleClaimReport(claim_terms=claim_terms, unsupported_terms=unsupported)


def _term_supported(term: str, normalized_signals: str) -> bool:
    if term not in _CLAIM_SUPPORT:  # a year or duration: the digits are the evidence
        return term in normalized_signals
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


def _normalize_tier(value: object) -> str:
    lowered = str(value or "").casefold()
    return next((tier for tier in _TIER_RANK if tier in lowered), "yellow")


def _format_terms(terms: list[str]) -> str:
    return ", ".join(f"'{term}'" for term in terms)


def guard_options(
    *,
    title_options: list[str],
    signals_text: str,
    option_tiers: list[str] | None = None,
    option_notes: list[str] | None = None,
    claim_supports: list[str] | None = None,
    recommended_index: int | None = None,
    recommendation_reason: str | None = None,
) -> GuardedDraftOptions:
    """Run the grounding check over every title and enforce the recommendation.

    With ``option_tiers`` provided (Groq path) the guard only downgrades:
    an option whose claim has no support becomes red, clean options keep the
    model's tier. Without tiers (clipboard path) the guard derives them: green
    when a title makes no checkable claim, yellow when its claims are
    supported, red when one is not. If the recommended pick lands on a red
    option, the recommendation moves to the strongest clean option instead —
    the flagged option is kept (not rewritten) so the user can still choose it.
    """
    flagged: dict[int, list[str]] = {}
    tiers: list[str] = []
    notes = [
        option_notes[i] if option_notes and i < len(option_notes) else ""
        for i in range(len(title_options))
    ]
    for i, title in enumerate(title_options):
        citation = claim_supports[i] if claim_supports and i < len(claim_supports) else None
        report = check_title_claims(title, signals_text, citation)
        if option_tiers and i < len(option_tiers):
            tier = _normalize_tier(option_tiers[i])
        else:
            tier = "green" if not report.claim_terms else "yellow"
        if report.unsupported_terms:
            flagged[i] = report.unsupported_terms
            tier = "red"
            warning = (
                f"Grounding check: {_format_terms(report.unsupported_terms)} not backed by "
                "the transcript, source caption, or visual evidence; verify before posting."
            )
            notes[i] = f"{notes[i]} [{warning}]".strip() if notes[i] else warning
        tiers.append(tier)

    new_index, reason, shifted = _enforce_recommendation(
        tiers=tiers,
        flagged=flagged,
        recommended_index=recommended_index,
        recommendation_reason=recommendation_reason,
    )
    return GuardedDraftOptions(
        option_tiers=tiers,
        option_notes=notes if any(notes) else None,
        recommended_index=new_index,
        recommendation_reason=reason,
        flagged_terms=flagged,
        recommendation_shifted=shifted,
    )


def _enforce_recommendation(
    *,
    tiers: list[str],
    flagged: dict[int, list[str]],
    recommended_index: int | None,
    recommendation_reason: str | None,
) -> tuple[int | None, str | None, bool]:
    if recommended_index is None or not (0 <= recommended_index < len(tiers)):
        return recommended_index, recommendation_reason, False
    if tiers[recommended_index] != "red":
        return recommended_index, recommendation_reason, False

    terms = flagged.get(recommended_index)
    claim_text = (
        f"{_format_terms(terms)} has no support in the clip signals"
        if terms
        else "its title makes an unverifiable claim"
    )
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
