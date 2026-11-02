"""Judge a captured clip by its views against its creator's normal views.

Measured on 1,854 of the network's own posted reels (2026-09-26), each compared
with the other posts of the same account in the same week: a clip's source views
divided by its creator's median views predicted how it did for us better than raw
views, and it works the same for a small page as for a big one.

    views / creator median    <0.5x  0.5-1x  1-2x  2-5x  5-10x  10x+
    our within-week pctile      38     45     51    60    53     55

Two more measured points shape the verdict. Source clips past 3M views did worse
for us (percentile ~43 against ~56 for 100k-3M): the audience has usually seen
them already. Like and comment rates predicted nothing, so they are reported but
never scored.

Pure logic: callers look up the creator's baseline and decide what a verdict
does. Nothing here touches the database or the network.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Verdict = Literal["strong", "ok", "weak", "unknown"]

STRONG_RATIO = 2.0
OK_RATIO = 1.0
SEEN_ALREADY_VIEWS = 3_000_000
# Reel views keep climbing for a few days, so a young clip's ratio understates it.
STILL_RISING_DAYS = 3
# With fewer recent reels than this the median is too noisy to judge against.
MIN_BASELINE_POSTS = 5

# Unknown sorts above weak: "not judged yet" deserves a look before "judged poor".
VERDICT_RANK: dict[str, int] = {"strong": 0, "ok": 1, "unknown": 2, "weak": 3}

# The standing content exclusions, matched as phrases rather than through
# classify_topic_tier, whose "oscilloscope" keyword also catches Tennis for Two.
_EXCLUDED = re.compile(r"hydraulic press|physics demo", re.IGNORECASE)
# Paid promotion is held back. Narrow on purpose: words like "tickets" or
# "out now" also appear in ordinary captions about concerts and films.
_PROMO = re.compile(
    r"#ad\b|#sponsored\b|paid partnership|use (?:my )?code\b|promo code|discount code|giveaway",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClipCheck:
    verdict: Verdict
    views: int | None
    baseline_views: int | None
    ratio: float | None
    flags: tuple[str, ...]
    summary: str

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "ratio": round(self.ratio, 2) if self.ratio is not None else None,
            "baseline_views": self.baseline_views,
            "flags": list(self.flags),
            "summary": self.summary,
        }


def median_views(views: Iterable[int | None]) -> int | None:
    """A creator's normal views: the median of their recent reels, or None if too few."""
    values = [int(value) for value in views if value is not None and value > 0]
    if len(values) < MIN_BASELINE_POSTS:
        return None
    return int(statistics.median(values))


def check_clip(
    *,
    views: int | None,
    likes: int | None,
    comments: int | None,
    published_at: dt.datetime | None,
    channel_name: str | None,
    text: str | None,
    baseline_views: int | None,
    now: dt.datetime | None = None,
) -> ClipCheck:
    """Verdict for one clip. ``text`` is the caption, used for the exclusion checks."""
    age_days = _age_days(published_at, now)
    ratio = views / baseline_views if views and baseline_views else None
    flags: list[str] = []
    body = text or ""

    verdict: Verdict
    if _EXCLUDED.search(body):
        verdict = "weak"
        flags.append("excluded content (hydraulic press or physics demo)")
    elif _PROMO.search(body):
        verdict = "weak"
        flags.append("looks like a paid promotion")
    elif ratio is None:
        verdict = "unknown"
        flags.append("no view count" if not views else "creator's normal views unknown")
    elif ratio >= STRONG_RATIO:
        verdict = "strong"
    elif ratio >= OK_RATIO:
        verdict = "ok"
    else:
        verdict = "weak"

    if views is not None and views >= SEEN_ALREADY_VIEWS:
        flags.append("3M+ views, probably seen already")
        if verdict == "strong":
            verdict = "ok"
    if age_days is not None and age_days < STILL_RISING_DAYS:
        flags.append("still rising")

    summary = _summary(
        verdict,
        views=views,
        likes=likes,
        comments=comments,
        baseline_views=baseline_views,
        ratio=ratio,
        age_days=age_days,
        channel_name=channel_name,
        flags=flags,
    )
    return ClipCheck(
        verdict=verdict,
        views=views,
        baseline_views=baseline_views,
        ratio=ratio,
        flags=tuple(flags),
        summary=summary,
    )


def _age_days(published_at: dt.datetime | None, now: dt.datetime | None) -> float | None:
    if published_at is None:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    published = published_at if published_at.tzinfo else published_at.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (current - published).total_seconds() / 86_400)


def compact_number(value: int) -> str:
    """412345 -> "412k", 3_400_000 -> "3.4M", 1_250 -> "1.2k"."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if value >= 10_000:
        return f"{value // 1_000}k"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(value)


def _age_label(age_days: float) -> str:
    days = int(age_days)
    if days < 1:
        return "posted today"
    return "1 day old" if days == 1 else f"{days} days old"


def _summary(
    verdict: Verdict,
    *,
    views: int | None,
    likes: int | None,
    comments: int | None,
    baseline_views: int | None,
    ratio: float | None,
    age_days: float | None,
    channel_name: str | None,
    flags: list[str],
) -> str:
    # ASCII separators: summaries also reach Windows consoles, whose codepage
    # mangles typographic characters.
    parts: list[str] = []
    if ratio is not None and views and baseline_views:
        creator = f"@{channel_name}" if channel_name else "the creator"
        parts.append(
            f"{ratio:.1f}x normal for {creator} "
            f"({compact_number(views)} vs {compact_number(baseline_views)})"
        )
    elif views:
        parts.append(f"{compact_number(views)} views")
    # Instagram reports hidden like counts as negative numbers.
    if likes is not None and likes >= 0:
        parts.append(f"{compact_number(likes)} likes")
    if comments is not None and comments >= 0:
        parts.append(f"{compact_number(comments)} comments")
    if age_days is not None:
        parts.append(_age_label(age_days))
    parts.extend(flags)
    label = "OK" if verdict == "ok" else verdict.capitalize()
    return f"{label}: " + " | ".join(parts)
