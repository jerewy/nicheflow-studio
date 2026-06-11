"""Pure scheduling helpers for assigning post times from per-account slots.

Kept free of UI/DB so it is easy to test and reuse. The key idea: turn an
account's daily slots (e.g. "09:00, 18:00") into concrete future datetimes,
adding randomized minute/second jitter so posts never land on robotic round
numbers (a bot tell).
"""

from __future__ import annotations

import random as _random
from datetime import datetime, timedelta

# Posts land 0..jitter after the round slot moment. Wide enough that a daily
# slot doesn't produce a recognizable "always 09:0x" pattern, narrow enough
# that multi-hour slot spacing never collides (collision guard = jitter + 5).
DEFAULT_JITTER_MINUTES = 15
DEFAULT_CATCH_UP_GRACE_HOURS = 3
DEFAULT_CATCH_UP_MIN_GAP_HOURS = 2
_CATCH_UP_DELAY_MINUTES = (5, 20)


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


def next_open_slot_time(
    slots: str | None,
    *,
    after: datetime,
    occupied: list[datetime] | tuple[datetime, ...] = (),
    jitter_minutes: int = DEFAULT_JITTER_MINUTES,
    rng: _random.Random | None = None,
    collision_minutes: int | None = None,
    max_days: int = 60,
) -> datetime | None:
    """Return the next future slot time that isn't already taken, jittered.

    Walks this account's slots forward from ``after`` and returns the first slot
    that no existing post occupies. A slot counts as occupied when any datetime
    in ``occupied`` (e.g. the account's already-scheduled posts) lands within
    ``collision_minutes`` of the round slot moment — so auto-scheduling several
    exports in a row spreads them across distinct slots/days instead of stacking
    two reels on the same minute. The returned time has the same 0..jitter
    offset treatment as :func:`upcoming_slot_times` so it never lands on a
    robotic round number. Returns ``None`` when there are no valid slots or none
    is open within ``max_days``.
    """
    parsed = parse_slots(slots)
    if not parsed:
        return None
    rng = rng or _random
    jitter_seconds = max(0, jitter_minutes) * 60
    if collision_minutes is None:
        # Guard window: a candidate within jitter range of an existing post could
        # collide once jitter is applied, so keep at least the jitter span plus a
        # small buffer between posts.
        collision_minutes = max(jitter_minutes, 1) + 5
    collision_seconds = max(0, collision_minutes) * 60

    day = after.date()
    days_checked = 0
    while days_checked <= max_days:
        for hour, minute in parsed:
            base = datetime(day.year, day.month, day.day, hour, minute, tzinfo=after.tzinfo)
            if base <= after:
                continue
            if any(
                abs((taken - base).total_seconds()) <= collision_seconds
                for taken in occupied
            ):
                continue
            offset = rng.randint(0, jitter_seconds) if jitter_seconds else 0
            return base + timedelta(seconds=offset)
        day = day + timedelta(days=1)
        days_checked += 1
    return None


def most_recent_passed_slot_time(slots: str | None, *, now: datetime) -> datetime | None:
    """Return today's latest configured slot at or before ``now``."""
    passed = [
        datetime(now.year, now.month, now.day, hour, minute, tzinfo=now.tzinfo)
        for hour, minute in parse_slots(slots)
        if (hour, minute) <= (now.hour, now.minute)
    ]
    return max(passed, default=None)


def catch_up_slot_time(
    slots: str | None,
    *,
    now: datetime,
    occupied: list[datetime] | tuple[datetime, ...],
    last_posted_at: datetime | None,
    grace_hours: float = DEFAULT_CATCH_UP_GRACE_HOURS,
    min_gap_hours: float = DEFAULT_CATCH_UP_MIN_GAP_HOURS,
    checkpoint_cooldown: bool = False,
    rng: _random.Random | None = None,
    collision_minutes: int = DEFAULT_JITTER_MINUTES + 5,
) -> datetime | None:
    """Return a delayed catch-up time when a recent missed slot is safe to recover.

    ``occupied`` includes both scheduled and actually-posted job timestamps.
    This helper makes no persistence decisions; callers still queue the returned
    time through the normal scheduler.
    """
    if checkpoint_cooldown:
        return None

    missed_slot = most_recent_passed_slot_time(slots, now=now)
    if missed_slot is None or now - missed_slot > timedelta(hours=max(0, grace_hours)):
        return None

    collision = timedelta(minutes=max(0, collision_minutes))
    if any(abs(taken - missed_slot) <= collision for taken in occupied):
        return None

    if (
        last_posted_at is not None
        and now - last_posted_at < timedelta(hours=max(0, min_gap_hours))
    ):
        return None

    forward_slots = upcoming_slot_times(slots, 1, after=now, jitter_minutes=0)
    if not forward_slots:
        return None
    next_forward_slot = forward_slots[0]
    if any(now < taken < next_forward_slot for taken in occupied):
        return None

    rng = rng or _random
    delay_minutes = rng.randint(*_CATCH_UP_DELAY_MINUTES)
    return now + timedelta(minutes=delay_minutes)
