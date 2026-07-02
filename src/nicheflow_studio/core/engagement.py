"""Shared public-metric scoring for pool review and distribution."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from nicheflow_studio.core.distribution import DEFAULT_RECENCY_HALF_LIFE_DAYS

TopicTier = Literal["S", "A", "B", "C", "D"]
SuggestedAction = Literal["accept", "review", "reject"]

TOPIC_TIER_WEIGHTS: dict[TopicTier, float] = {
    "S": 1.6,
    "A": 1.3,
    "B": 1.0,
    "C": 0.5,
    "D": 0.0,
}

_TOPIC_KEYWORDS: tuple[tuple[TopicTier, tuple[str, ...]], ...] = (
    ("D", ("hydraulic press", "physics demo", "oscilloscope", "raw trivia")),
    (
        "C",
        (
            "spotted",
            "appearance",
            "walked on stage",
            "confidence",
            "reaction",
            "funeral procession",
            "photographed",
        ),
    ),
    (
        "A",
        (
            "cartoon",
            "childhood",
            "theme song",
            "classic",
            "remember",
            "that time",
            "for the first time",
        ),
    ),
    (
        "S",
        (
            "performance",
            "sang",
            "song",
            "concert",
            "stage",
            "tribute",
            "vma",
            "duet",
            "grief",
            "loss",
            "reunion",
            "wrote for",
            "last time",
            "decades later",
            "returned",
        ),
    ),
    ("B", ("sports", "match", "record", "behind the scenes")),
)


def source_engagement_rate(*, views: int | None, likes: int | None, comments: int | None) -> float:
    """Return public engagement per view for a source candidate."""
    engagement = max(0, int(likes or 0)) + max(0, int(comments or 0))
    return engagement / max(1, int(views or 0))


def classify_topic_tier(text: str | None) -> TopicTier:
    """Classify title and caption text using the plan's lightweight seed map.

    Phrase-specific rejection and appearance categories are checked before broad
    words such as ``stage``. Unmatched text stays in B for human review instead
    of being rejected merely because the initial keyword map is incomplete.
    """
    normalized = " ".join((text or "").casefold().split())
    for tier, keywords in _TOPIC_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return tier
    return "B"


def recency_weight(
    published_at: dt.datetime | None,
    *,
    now: dt.datetime | None = None,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Use the existing evergreen-safe recency curve (range 0.5 to 1.0)."""
    if published_at is None or half_life_days <= 0:
        return 1.0
    current = (now or dt.datetime.now(dt.timezone.utc)).replace(tzinfo=None)
    published = published_at.replace(tzinfo=None)
    age_days = max(0.0, (current - published).total_seconds() / 86_400.0)
    decay = 0.5 ** (age_days / half_life_days)
    return 0.5 + 0.5 * decay


def source_fit_score(
    *,
    tier: TopicTier,
    source_er: float,
    published_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> float:
    """Rank source candidates by topic weight, public ER, and recency."""
    return TOPIC_TIER_WEIGHTS[tier] * max(0.0, source_er) * recency_weight(published_at, now=now)


# Advisory thresholds for suggested_action. A clip past SHORT_MAX_SECONDS is no
# longer rejected on length alone: only "long AND weakly engaged" is the real
# reject case. A long clip with strong public engagement is still worth posting
# (validated on the historytrails pool, where view counts hold and engagement
# rate rises past 35s). Engagement never forces a reject by itself; within the
# short cap it only splits accept vs review.
SHORT_MAX_SECONDS = 35
STRONG_SOURCE_ER = 0.03


def suggested_action(
    tier: TopicTier,
    *,
    source_er: float,
    duration_seconds: int | None = None,
) -> SuggestedAction:
    """Return an advisory action; callers must not mutate acceptance state."""
    if tier in {"C", "D"}:
        return "reject"
    strong_engagement = source_er >= STRONG_SOURCE_ER
    is_long = duration_seconds is not None and duration_seconds > SHORT_MAX_SECONDS
    # Length only counts against a clip when engagement is also weak; a long clip
    # that already engages well stays accept.
    if is_long and not strong_engagement:
        return "reject"
    return "accept" if strong_engagement else "review"
