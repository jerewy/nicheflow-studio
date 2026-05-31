from datetime import datetime, timedelta, timezone

import nicheflow_studio.core.account_health as ah
from nicheflow_studio.core.account_health import HealthState, live_health, local_health
from nicheflow_studio.core.instagram_profile_pool import Profile, ProfilePool

NOW = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)


def _pool(tmp_path, **profile_kwargs) -> ProfilePool:
    return ProfilePool(
        manifest_path=tmp_path / "profiles.json",
        profiles={"main": Profile(name="main", **profile_kwargs)},
    )


def test_local_health_no_cookie_is_no_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: False)
    health = local_health("main", "Acct", pool=_pool(tmp_path), now=NOW)
    assert health.state == HealthState.NO_SESSION
    assert health.is_live is False
    assert health.needs_attention is True


def test_local_health_fresh_login_is_ok(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    pool = _pool(tmp_path, last_login=NOW - timedelta(days=1))
    health = local_health("main", "Acct", pool=pool, now=NOW)
    assert health.state == HealthState.OK
    assert health.needs_attention is False


def test_local_health_warns_when_login_aging(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    pool = _pool(tmp_path, last_login=NOW - timedelta(days=6))
    assert local_health("main", pool=pool, now=NOW).state == HealthState.WARN


def test_local_health_stale_when_login_old(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    pool = _pool(tmp_path, last_login=NOW - timedelta(days=20))
    health = local_health("main", pool=pool, now=NOW)
    assert health.state == HealthState.STALE
    assert health.needs_attention is True


def test_local_health_reports_cooldown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    pool = _pool(
        tmp_path,
        last_login=NOW - timedelta(days=1),
        cooldown_until=NOW + timedelta(hours=2),
    )
    assert local_health("main", pool=pool, now=NOW).state == HealthState.COOLDOWN


def test_local_health_unknown_without_login_date(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    pool = _pool(tmp_path)  # no last_login
    assert local_health("main", pool=pool, now=NOW).state == HealthState.UNKNOWN


def _live_pool(tmp_path) -> ProfilePool:
    return _pool(tmp_path, last_login=NOW - timedelta(days=1))


def test_live_health_flags_wrong_account_as_mismatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    monkeypatch.setattr(ah, "_live_identify", lambda name: ("memeistsdaily", None))
    health = live_health(
        "alt1", "Past Moments Daily",
        expected_username="pastmomentsdaily", pool=_live_pool(tmp_path), now=NOW,
    )
    assert health.state == HealthState.MISMATCH
    assert health.needs_attention is True
    assert "memeistsdaily" in health.detail and "pastmomentsdaily" in health.detail


def test_live_health_matching_handle_is_ok(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    monkeypatch.setattr(ah, "_live_identify", lambda name: ("memeistsdaily", None))
    # Expected handle is compared case-insensitively and ignores a leading '@'.
    health = live_health(
        "main", "Memeists Daily",
        expected_username="@MemeistsDaily", pool=_live_pool(tmp_path), now=NOW,
    )
    assert health.state == HealthState.OK


def test_live_health_without_expected_handle_just_confirms(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ah, "_has_sessionid", lambda name: True)
    monkeypatch.setattr(ah, "_live_identify", lambda name: ("whoever", None))
    health = live_health("main", "Acct", pool=_live_pool(tmp_path), now=NOW)
    assert health.state == HealthState.OK
    assert health.username == "whoever"
