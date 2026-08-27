"""Shared public-metric scoring for pool review and distribution."""

from __future__ import annotations

import datetime as dt
import math
from typing import Literal

from nicheflow_studio.core.distribution import DEFAULT_RECENCY_HALF_LIFE_DAYS

TopicTier = Literal["S", "A", "B", "C", "D"]
SuggestedAction = Literal["accept", "review", "reject"]

# Graded tier weights are retired. Measured over 1,100 posted reels with real
# Instagram insights (2026-08-12), the tiers do not separate outcomes at all:
#
#   tier A  n=200  median 4,852     tier B  n=381  median 4,678
#   tier C  n= 40  median 5,009     tier S  n=324  median 4,592
#
# C was penalised x0.5 and performed BEST; S was boosted x1.6 and performed
# worst. The weights were amplifying a hand-seeded ~40-keyword guess, so they
# are flattened to 1.0 rather than re-fitted to noise.
#
# D stays 0.0: that is a deliberate content exclusion ("hydraulic press",
# "physics demo", "oscilloscope", "raw trivia" — off-niche formats), not a
# performance prediction, so the measurement above does not bear on it.
TOPIC_TIER_WEIGHTS: dict[TopicTier, float] = {
    "S": 1.0,
    "A": 1.0,
    "B": 1.0,
    "C": 1.0,
    "D": 0.0,
}

# How much the source's engagement RATE may swing a score, as a multiplier on
# top of reach. Source ER does carry signal (Spearman +0.173 against real
# views), but less than absolute reach (+0.288) and it is not monotonic: its
# top quartile underperforms its third. So it is a tiebreaker, capped, never a
# primary term.
_ER_TIEBREAK_WEIGHT = 2.0
_ER_TIEBREAK_CAP = 0.10

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
    source_views: int | None = None,
    published_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> float:
    """Rank source candidates by how far the footage already travelled.

    Reach-first. The previous formula was ``tier x source_er x recency``, which
    made engagement RATE the only real term — and rate divides reach away, so a
    300-view clip with 60 likes outranked a 2M-view clip. Measured against 1,100
    of the network's own posted reels (2026-08-12), that was backwards:

        ranked by source ER      Q1 3,634  Q2 4,449  Q3 6,010  Q4 5,360
        ranked by source VIEWS   Q1 3,368  Q2 4,271  Q3 5,595  Q4 8,256

    Source views is monotonic and spreads 2.45x bottom-to-top quartile; source
    ER peaks in Q3 and falls. Correlations against real views: source likes
    +0.293, source views +0.288, source ER +0.173.

    Reach enters as log10 so the term compresses: a 1000x reach difference is
    worth ~2x score, which keeps one viral source clip from monopolising the
    whole distribution. ER survives only as a capped tiebreaker.

    ``source_views`` is optional so a candidate whose public metrics never
    scraped still ranks (on ER and recency alone) instead of scoring zero and
    sinking below every measured clip.
    """
    if source_views is None:
        reach = 1.0
    else:
        reach = math.log10(1.0 + max(0, int(source_views)))
    tiebreak = 1.0 + _ER_TIEBREAK_WEIGHT * min(max(0.0, source_er), _ER_TIEBREAK_CAP)
    return TOPIC_TIER_WEIGHTS[tier] * reach * tiebreak * recency_weight(published_at, now=now)


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
    # C used to be auto-rejected alongside D. Measured over 1,100 posted reels
    # (2026-08-12), tier C posts had the HIGHEST median views of any tier
    # (5,009 vs S 4,592), so rejecting them on the keyword label alone was
    # advice against the evidence. C now falls through to the engagement and
    # length checks like any other clip. D remains an outright reject: it is a
    # content exclusion (hydraulic press, physics demos, raw trivia), not a
    # performance call.
    if tier == "D":
        return "reject"
    strong_engagement = source_er >= STRONG_SOURCE_ER
    is_long = duration_seconds is not None and duration_seconds > SHORT_MAX_SECONDS
    # Length only counts against a clip when engagement is also weak; a long clip
    # that already engages well stays accept.
    if is_long and not strong_engagement:
        return "reject"
    return "accept" if strong_engagement else "review"
