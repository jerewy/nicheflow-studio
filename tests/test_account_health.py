from datetime import datetime, timedelta, timezone

import nicheflow_studio.core.account_health as ah
from nicheflow_studio.core.account_health import HealthState, local_health
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
