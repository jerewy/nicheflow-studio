"""Cheap, pre-download text signals for catching reposted clips.

Reposting accounts in the same niche often copy each other's captions, so a
normalized caption is a free way to spot likely duplicates *before* spending any
download budget (SOURCING_POOLING_PLAN.md §3, dedup-before-download). This is an
approximate filter — the reliable footage check happens after download — so it
deliberately errs toward only matching near-identical text.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_caption(text: str | None) -> str:
    """Reduce a caption to a comparison key: lowercased, emoji/punctuation
    stripped, whitespace collapsed. Returns ``""`` for empty/None input.

    Two captions that share a normalized key are treated as the same repost.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = _NON_ALNUM.sub(" ", folded)  # drop emoji, punctuation, symbols
    return _WHITESPACE.sub(" ", folded).strip()
