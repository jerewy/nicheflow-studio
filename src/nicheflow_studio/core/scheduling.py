"""Pure scheduling helpers for assigning post times from per-account slots.

Kept free of UI/DB so it is easy to test and reuse. The key idea: turn an
account's daily slots (e.g. "09:00, 18:00") into concrete future datetimes,
adding randomized minute/second jitter so posts never land on robotic round
numbers (a bot tell).
"""

from __future__ import annotations

import random as _random
from datetime import datetime, timedelta

DEFAULT_JITTER_MINUTES = 7


def parse_slots(slots: str | None) -> list[tuple[int, int]]:
    """Parse "09:00, 18:00" into sorted unique (hour, minute) pairs."""
    parsed: set[tuple[int, int]] = set()
    for raw in (slots or "").replace("\n", ",").replace(";", ",").split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            hour_text, minute_text = token.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed.add((hour, minute))
    return sorted(parsed)


def upcoming_slot_times(
    slots: str | None,
    count: int,
    *,
    after: datetime,
    jitter_minutes: int = DEFAULT_JITTER_MINUTES,
    rng: _random.Random | None = None,
    max_days: int = 60,
) -> list[datetime]:
    """Return ``count`` future post times from ``slots``, each jittered.

    Times roll across days as slots fill, so N jobs spread out instead of
    stacking on one moment. Each time is a slot moment plus a random 0..jitter
    offset (so it lands a few minutes/seconds after the round slot, never on
    it). Returns ``[]`` when there are no valid slots.
    """
    parsed = parse_slots(slots)
    if not parsed or count <= 0:
        return []
    rng = rng or _random
    jitter_seconds = max(0, jitter_minutes) * 60

    result: list[datetime] = []
    day = after.date()
    days_checked = 0
    while len(result) < count and days_checked <= max_days:
        for hour, minute in parsed:
            base = datetime(day.year, day.month, day.day, hour, minute, tzinfo=after.tzinfo)
            if base <= after:
                continue
            offset = rng.randint(0, jitter_seconds) if jitter_seconds else 0
            result.append(base + timedelta(seconds=offset))
            if len(result) >= count:
                break
        day = day + timedelta(days=1)
        days_checked += 1
    return result[:count]
