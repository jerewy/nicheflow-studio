"""Niche classification for the shared-pool network.

The network keeps two strictly isolated content niches — ``history`` and
``movie`` — so a clip never crosses from one pool to the other
(docs/SOURCING_POOLING_PLAN.md §7). This module turns a free-text label
(``Account.niche_label``) into that strict niche, kept pure so it is easy to
test and reuse from migrations, importers, and assignment guards.
"""
from __future__ import annotations

NICHE_HISTORY = "history"
NICHE_MOVIE = "movie"

_HISTORY_KEYWORDS = (
    "history",
    "historical",
    "vintage",
    "archive",
    "archival",
    "old footage",
    "retro",
    "past moments",
)
_MOVIE_KEYWORDS = (
    "movie",
    "film",
    "cinema",
    "cinematic",
    "scene",
)


def classify_niche(niche_label: str | None) -> str | None:
    """Best-effort strict niche from a free-text label.

    Returns ``"history"``, ``"movie"``, or ``None`` when the label doesn't
    clearly belong to either (e.g. a meme account). History is checked first so
    a "movie history" style label leans history; callers that need certainty
    should set ``Account.niche`` explicitly.
    """
    text = (niche_label or "").strip().lower()
    if not text:
        return None
    if any(keyword in text for keyword in _HISTORY_KEYWORDS):
        return NICHE_HISTORY
    if any(keyword in text for keyword in _MOVIE_KEYWORDS):
        return NICHE_MOVIE
    return None
