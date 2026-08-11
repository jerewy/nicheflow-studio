"""Measured on-screen title examples, retrieved per generation.

The title prompt used to carry five hardcoded example titles, identical for
every clip and every account. Two problems followed from that.

The first is repetition. Across the last 360 generated titles, 50 opened with
"It looked like" or "It looks like" and 27 contained ", until", because a fixed
example set is a fixed attractor. Rotating the examples per generation is the
cheapest fix available: models copy examples far more reliably than they follow
prose rules, which is the same reason the existing "use THREE DIFFERENT shapes"
instruction never took hold.

The second is register. All five static examples were documentary museum labels
("21-year-old Adam Sandler during spring break in Fort Lauderdale, 1988..."),
which is the account's *weakest* measured register. Scored over 726 real
@historytrails titles:

    question mark        187k median views   vs 73k for the rest
    first person         95k                 vs 73k
    starts "This ..."    89k                 vs 73k
    has a 4-digit year   72k                 vs 76k
    superlative          61k                 vs 76k
    starts "When ..."    54k                 vs 74k

So the prompt was teaching the lowest-performing voice on every generation. The
buckets here exist to force a mix instead.

Caveat worth keeping in mind before treating these as laws: topic drives reach
enormously (a cat clip outperforms on being a cat, not on its title shape), and
these are observational correlations over one account, not a controlled test.
They are a better prior than five arbitrary lines, not proof.

The source CSV lives under ``data/`` and is gitignored, so a fresh clone or a
packaged build will not have it. Every entry point degrades to the caller's
static fallback rather than failing.
"""

from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from nicheflow_studio.core.paths import data_dir

REFERENCE_CSV_RELATIVE = Path("title_analysis/historytrails-ocr/title_analysis_summary.csv")

# Registers, in the order they are drawn for a prompt. Question and first-person
# lead because they measure strongest; documentary stays represented because the
# account's identity depends on it and its median is respectable.
REGISTERS = ("question", "first_person", "short_label", "documentary")

_SHORT_LABEL_MAX_WORDS = 6
_MIN_TITLE_CHARS = 14
# Only the strongest slice of each bucket is eligible, so rotation varies the
# examples without ever reaching down into mediocre ones.
_BUCKET_POOL_SIZE = 25

# The reference titles come from OCR over screenshots, so a slice of them have
# words glued together ("Thisishowtheworld'sbestsoccerballs"). Structure is what
# these examples teach, and glued text would teach broken spacing, so anything
# that looks mis-segmented is dropped rather than repaired.
#
# These are shape heuristics, not a spellcheck: a short glue of two common words
# ("theroom", "gavehera") has no length, capital, or symbol tell and survives.
# OCR confidence does not catch them either, because the scan is confident and
# correct about the pixels; the source video simply sets the text tightly. The
# residual is tolerated because these examples are labelled in the prompt as
# illustrating structure only, so a typo in one costs far less than the
# dictionary it would take to catch it.
_MAX_WORD_CHARS = 12
_MAX_AVERAGE_WORD_CHARS = 7.5
_INNER_CAPITAL_RE = re.compile(r"[a-z][A-Z]")
# A lone letter that is not "a" or "I" is an OCR fragment, not a word: it shows
# up where the scan split a word or picked up part of an on-screen graphic
# ("the capsule is at 2 2800 d", "anxiety crisis S during reentry").
_STRAY_LETTER_RE = re.compile(r"(?:^|\s)(?![aAI](?:$|\s))[A-Za-z](?:$|\s)")
# An apostrophe followed by anything other than a real contraction suffix means
# the scan swallowed a space ("won'tbe mad", "Hold 'dn").
_BAD_CONTRACTION_RE = re.compile(r"'(?!(?:t|s|re|ve|ll|d|m|clock)\b)[A-Za-z]+", re.IGNORECASE)
# Letters welded to digits ("with911a about a fire") is always a merged box.
_LETTER_DIGIT_MIX_RE = re.compile(r"\b(?=\w*[A-Za-z])(?=\w*\d)\w+\b")
_FIRST_PERSON_RE = re.compile(r"\b(i|my|me|we|our|us)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ReferenceTitle:
    text: str
    views: float
    engagement: float
    word_count: int
    register: str


def classify_register(text: str) -> str:
    """Bucket a title by the voice it speaks in, not by its topic."""
    if "?" in text:
        return "question"
    if _FIRST_PERSON_RE.search(text):
        return "first_person"
    if len(text.split()) <= _SHORT_LABEL_MAX_WORDS:
        return "short_label"
    return "documentary"


def _is_cleanly_segmented(text: str) -> bool:
    words = text.split()
    if not words:
        return False
    if any(len(word) > _MAX_WORD_CHARS for word in words):
        return False
    if (
        _INNER_CAPITAL_RE.search(text)
        or _STRAY_LETTER_RE.search(text)
        or _BAD_CONTRACTION_RE.search(text)
        or _LETTER_DIGIT_MIX_RE.search(text)
    ):
        return False
    return sum(len(word) for word in words) / len(words) <= _MAX_AVERAGE_WORD_CHARS


@lru_cache(maxsize=1)
def load_reference_titles() -> tuple[ReferenceTitle, ...]:
    """Read and clean the scored reference titles. Empty when the CSV is absent."""
    csv_path = data_dir() / REFERENCE_CSV_RELATIVE
    if not csv_path.exists():
        return ()

    entries: list[ReferenceTitle] = []
    try:
        with csv_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                text = " ".join((row.get("on_screen_title") or "").split())
                if len(text) < _MIN_TITLE_CHARS or not _is_cleanly_segmented(text):
                    continue
                try:
                    views = float(row["view_count"])
                    engagement = float(row["engagement_rate"])
                    word_count = int(row["word_count"])
                except (KeyError, TypeError, ValueError):
                    continue
                if views <= 0:
                    continue
                entries.append(
                    ReferenceTitle(
                        text=text,
                        views=views,
                        engagement=engagement,
                        word_count=word_count,
                        register=classify_register(text),
                    )
                )
    except OSError:
        return ()
    return tuple(entries)


def select_reference_titles(*, count: int = 8, seed: int | None = None) -> list[str]:
    """Pick a rotating, register-balanced set of measured example titles.

    Returns an empty list when no reference data is available, which is the
    signal for the caller to keep its static examples.
    """
    entries = load_reference_titles()
    if not entries or count <= 0:
        return []

    pools: dict[str, list[ReferenceTitle]] = {}
    for register in REGISTERS:
        ranked = sorted(
            (entry for entry in entries if entry.register == register),
            key=lambda entry: entry.views,
            reverse=True,
        )
        if ranked:
            pools[register] = ranked[:_BUCKET_POOL_SIZE]
    if not pools:
        return []

    rng = random.Random(seed)
    picked: list[str] = []
    seen: set[str] = set()
    # Round-robin so a thin bucket (there are only ~15 usable questions in the
    # set) never starves the mix, and a fat one never dominates it.
    while len(picked) < count:
        added = False
        for register in REGISTERS:
            if len(picked) >= count:
                break
            pool = pools.get(register)
            if not pool:
                continue
            available = [entry for entry in pool if entry.text not in seen]
            if not available:
                continue
            choice = rng.choice(available)
            seen.add(choice.text)
            picked.append(choice.text)
            added = True
        if not added:
            break
    return picked
