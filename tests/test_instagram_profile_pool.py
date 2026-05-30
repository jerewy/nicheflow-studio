from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nicheflow_studio.core import instagram_profile_pool as pool_module
from nicheflow_studio.core import instagram_session
from nicheflow_studio.core.instagram_profile_pool import (
    NoAvailableProfilesError,
    Profile,
    ProfilePool,
)


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch):
    """Redirect the profile root to a tmp dir and return helpers to seed profiles."""
    root = tmp_path / "instagram"
    root.mkdir(parents=True)
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", root)
    monkeypatch.setattr(instagram_session, "_LEGACY_STORAGE_STATE_PATH", root / "storage-state.json")

    def make_profile(name: str) -> Path:
        profile_dir = root / name
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "storage-state.json").write_text("{\"cookies\":[]}", encoding="utf-8")
        return profile_dir

    return root, make_profile


def test_pool_picks_only_profile_when_one_exists(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    picked = pool.pick()

    assert picked.name == "main"


def test_pool_prefers_never_used_profile_over_recently_used(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    make("alt1")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_used("main")

    assert pool.pick().name == "alt1"


def test_pool_picks_least_recently_used(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    make("alt1")
    make("alt2")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    pool.mark_used("main", now=base)
    pool.mark_used("alt1", now=base + timedelta(hours=1))
    pool.mark_used("alt2", now=base + timedelta(hours=2))

    assert pool.pick().name == "main"


def test_pool_skips_cooled_down_profiles(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    make("alt1")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_rate_limited("main", hours=6)

    assert pool.pick().name == "alt1"


def test_pool_raises_when_all_profiles_cooled(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_rate_limited("main", hours=6)

    with pytest.raises(NoAvailableProfilesError):
        pool.pick()


def test_pool_raises_when_no_profiles_on_disk(isolated_profiles) -> None:
    root, _ = isolated_profiles
    pool = ProfilePool.load(manifest_path=root / "profiles.json")

    with pytest.raises(NoAvailableProfilesError):
        pool.pick()


def test_mark_used_increments_total_and_clears_failures(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_rate_limited("main", hours=6)
    pool.mark_used("main", count=40)

    main = pool.profiles["main"]
    assert main.total_requests == 40
    assert main.failure_count == 0


def test_manifest_round_trip_preserves_state(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    make("alt1")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_used("main", count=12)
    pool.mark_rate_limited("alt1", hours=2)

    reloaded = ProfilePool.load(manifest_path=root / "profiles.json")

    assert reloaded.profiles["main"].total_requests == 12
    assert reloaded.profiles["alt1"].cooldown_until is not None
    assert reloaded.profiles["alt1"].failure_count == 1


def test_pool_auto_registers_new_profile_on_disk(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_used("main")

    # User logs in to a new account between runs.
    make("alt1")
    reloaded = ProfilePool.load(manifest_path=root / "profiles.json")

    assert "alt1" in reloaded.profiles
    assert reloaded.pick().name == "alt1"


def test_pool_ignores_profile_dir_without_storage_state(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    (root / "halflogged").mkdir()  # dir exists but no storage-state.json

    pool = ProfilePool.load(manifest_path=root / "profiles.json")

    assert pool.pick().name == "main"


def test_resolve_profile_name_passes_through_explicit_name(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    monkeypatched_path = root / "profiles.json"

    # When the user passes an explicit name, the pool is not consulted.
    assert pool_module.resolve_profile_name("alt1") == "alt1"
    assert not monkeypatched_path.exists()


def test_resolve_profile_name_picks_from_pool_when_auto(isolated_profiles) -> None:
    _, make = isolated_profiles
    make("main")

    assert pool_module.resolve_profile_name("auto") == "main"


def test_manifest_atomic_write_does_not_leave_temp_files(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")

    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_used("main")

    leftovers = [
        p for p in root.iterdir()
        if p.name.startswith("profiles.json") and p.suffix not in {".json", ""}
    ]
    assert leftovers == []


def test_mark_login_clears_cooldown_and_failures(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_rate_limited("main", hours=6)

    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    pool.mark_login("main", now=now)

    main = pool.profiles["main"]
    assert main.last_login == now
    assert main.cooldown_until is None
    assert main.failure_count == 0


def test_session_warning_at_5_days(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    base = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    pool.mark_login("main", now=base)

    assert pool.session_warning("main", now=base) is None
    assert pool.session_warning("main", now=base + timedelta(days=4)) is None
    warning = pool.session_warning("main", now=base + timedelta(days=6))
    assert warning is not None and "soon" in warning
    assert pool.session_is_stale("main", now=base + timedelta(days=6)) is False


def test_session_stale_at_14_days(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    base = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    pool.mark_login("main", now=base)

    assert pool.session_is_stale("main", now=base + timedelta(days=14, seconds=1)) is True
    warning = pool.session_warning("main", now=base + timedelta(days=15))
    assert warning is not None and "almost certainly dead" in warning


def test_session_warning_when_no_login_recorded(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")

    warning = pool.session_warning("main")
    assert warning is not None and "no recorded login date" in warning


def test_last_login_round_trips_through_manifest(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    base = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_login("main", now=base)

    reloaded = ProfilePool.load(manifest_path=root / "profiles.json")
    assert reloaded.profiles["main"].last_login == base


def test_profile_to_dict_serializes_iso_z(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    pool.mark_used("main", now=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc))

    raw = json.loads((root / "profiles.json").read_text(encoding="utf-8"))
    assert raw["profiles"]["main"]["last_used"] == "2026-05-24T12:00:00Z"


# ---------------------------------------------------------------------------
# Rate-limit hardening: graduated cooldown + stale-session filter
# ---------------------------------------------------------------------------


def test_graduated_cooldown_first_429_is_30_minutes(isolated_profiles) -> None:
    """A profile's first rate-limit hit should only cool 30 minutes, not 6
    hours — gives 3-5x more daily throughput across the main/alt1/alt2 set.
    """
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    base = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    pool.mark_rate_limited("main", now=base)

    profile = pool.profiles["main"]
    assert profile.cooldown_until == base + timedelta(minutes=30)
    assert profile.failure_count == 1


def test_graduated_cooldown_second_429_is_2_hours(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    base = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    pool.mark_rate_limited("main", now=base)
    pool.mark_rate_limited("main", now=base + timedelta(hours=1))

    profile = pool.profiles["main"]
    assert profile.cooldown_until == base + timedelta(hours=1) + timedelta(hours=2)
    assert profile.failure_count == 2


def test_graduated_cooldown_third_429_is_6_hours(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    base = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    pool.mark_rate_limited("main", now=base)
    pool.mark_rate_limited("main", now=base + timedelta(hours=1))
    pool.mark_rate_limited("main", now=base + timedelta(hours=3))

    profile = pool.profiles["main"]
    assert profile.cooldown_until == base + timedelta(hours=3) + timedelta(hours=6)
    assert profile.failure_count == 3


def test_graduated_cooldown_respects_explicit_hours(isolated_profiles) -> None:
    """Callers that pass an explicit ``hours`` still get exactly that — needed
    for the scraper's consecutive-failure-limit branch which intentionally
    overrides the graduated default.
    """
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    base = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    pool.mark_rate_limited("main", hours=12.0, now=base)

    assert pool.profiles["main"].cooldown_until == base + timedelta(hours=12)


def test_available_excludes_stale_session_profiles(isolated_profiles) -> None:
    """Profiles whose login is >=14 days old must not be picked — burning a
    request to discover they're dead wastes the rate-limit budget.
    """
    root, make = isolated_profiles
    make("main")
    make("alt1")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    # main was logged in 20 days ago — stale; alt1 was logged in yesterday — fresh
    pool.mark_login("main", now=now - timedelta(days=20))
    pool.mark_login("alt1", now=now - timedelta(days=1))

    picked = pool.pick(now=now)

    assert picked.name == "alt1"


def test_available_raises_when_all_profiles_are_stale(isolated_profiles) -> None:
    root, make = isolated_profiles
    make("main")
    pool = ProfilePool.load(manifest_path=root / "profiles.json")
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    pool.mark_login("main", now=now - timedelta(days=30))

    with pytest.raises(NoAvailableProfilesError):
        pool.pick(now=now)
