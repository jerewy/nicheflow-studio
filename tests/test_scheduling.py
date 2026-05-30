import random
from datetime import datetime, timedelta, timezone

from nicheflow_studio.core.scheduling import parse_slots, upcoming_slot_times

AFTER = datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc)


def test_parse_slots_filters_and_sorts() -> None:
    assert parse_slots("18:00, 09:00; 9:00\n25:00, bad, 12:30") == [(9, 0), (12, 30), (18, 0)]
    assert parse_slots("") == []
    assert parse_slots(None) == []


def test_upcoming_slot_times_empty_without_slots() -> None:
    assert upcoming_slot_times("", 3, after=AFTER) == []
    assert upcoming_slot_times("09:00", 0, after=AFTER) == []


def test_upcoming_slot_times_are_future_ordered_and_spread_across_days() -> None:
    rng = random.Random(1)
    times = upcoming_slot_times("09:00, 18:00", 3, after=AFTER, rng=rng)

    assert len(times) == 3
    assert all(t > AFTER for t in times)
    assert times == sorted(times)  # chronological
    # 3 jobs over 2 daily slots -> third rolls to the next day.
    assert times[0].date() == AFTER.date()
    assert times[2].date() == AFTER.date() + timedelta(days=1)


def test_jitter_keeps_time_within_window_after_slot() -> None:
    rng = random.Random(7)
    [only] = upcoming_slot_times("09:00", 1, after=AFTER, jitter_minutes=7, rng=rng)
    slot = datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc)
    # Lands at or after the slot, within the 7-minute jitter window.
    assert slot <= only <= slot + timedelta(minutes=7)
    # Almost never exactly on the round slot (jitter adds seconds).
    assert (only - slot).total_seconds() > 0


def test_zero_jitter_lands_exactly_on_slot() -> None:
    [only] = upcoming_slot_times("09:00", 1, after=AFTER, jitter_minutes=0)
    assert only == datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc)
