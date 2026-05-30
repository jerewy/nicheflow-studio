"""Account session health — a decoupled boundary over login/session checks.

The UI and schedulers depend ONLY on :class:`SessionHealth` and the two entry
points here (:func:`local_health`, :func:`live_health`). Everything
Instagram/Playwright/instaloader-specific is hidden inside this module, so a
future tech-stack change (different browser engine, different auth client, even
a different platform) is contained to this one file.

Two check depths:
- ``local_health``: cookie presence + recorded login age + cooldown. No network,
  so zero bot-detection risk. Safe to call anytime / on a UI timer.
- ``live_health``: actually asks Instagram "who am I?". Authoritative but contacts
  the network, so callers should run it on demand and space it out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from nicheflow_studio.core.instagram_profile_pool import (
    SESSION_STALE_AFTER_DAYS,
    SESSION_WARN_AFTER_DAYS,
    ProfilePool,
)
from nicheflow_studio.core.instagram_session import (
    load_playwright_cookies_from_storage_state,
    load_playwright_cookies_into_instaloader,
)

log = logging.getLogger(__name__)


class HealthState:
    """Coarse session states the UI can colour/sort by. Strings keep this
    serialisable and decoupled from any UI enum."""

    OK = "ok"  # session present and (live) confirmed / (local) fresh
    WARN = "warn"  # login getting old; plan to re-login soon
    STALE = "stale"  # login almost certainly dead; re-login needed
    NO_SESSION = "no_session"  # no saved cookies for this profile
    COOLDOWN = "cooldown"  # profile is cooling down after rate-limit/failure
    THROTTLED = "throttled"  # live check hit Instagram throttling
    LOGGED_OUT = "logged_out"  # live check: cookies exist but Instagram rejected them
    UNKNOWN = "unknown"  # session present but freshness/identity not determinable


@dataclass(frozen=True)
class SessionHealth:
    """Decoupled DTO describing one account/profile's login health."""

    profile_name: str
    account_name: str | None
    state: str
    detail: str
    checked_at: datetime
    is_live: bool
    username: str | None = None
    login_age_days: float | None = None
    cooldown_until: datetime | None = None

    @property
    def needs_attention(self) -> bool:
        return self.state in {
            HealthState.STALE,
            HealthState.NO_SESSION,
            HealthState.THROTTLED,
            HealthState.LOGGED_OUT,
        }


def _has_sessionid(profile_name: str) -> bool:
    cookies = load_playwright_cookies_from_storage_state(profile_name)
    return any(c.get("name") == "sessionid" and c.get("value") for c in cookies)


def local_health(
    profile_name: str,
    account_name: str | None = None,
    *,
    pool: ProfilePool | None = None,
    now: datetime | None = None,
) -> SessionHealth:
    """Network-free health for one profile. Safe to call freely."""
    moment = now or datetime.now(timezone.utc)
    pool = pool or ProfilePool.load()
    profile = pool.profiles.get(profile_name)
    cooldown_until = profile.cooldown_until if profile else None
    login_age = profile.login_age_days(moment) if profile else None

    def build(state: str, detail: str) -> SessionHealth:
        return SessionHealth(
            profile_name=profile_name,
            account_name=account_name,
            state=state,
            detail=detail,
            checked_at=moment,
            is_live=False,
            login_age_days=login_age,
            cooldown_until=cooldown_until,
        )

    if not _has_sessionid(profile_name):
        return build(
            HealthState.NO_SESSION,
            "No saved login. Run the login script for this profile.",
        )
    if cooldown_until is not None and cooldown_until > moment:
        return build(
            HealthState.COOLDOWN,
            f"Cooling down until {cooldown_until.astimezone():%Y-%m-%d %H:%M}.",
        )
    if login_age is None:
        return build(HealthState.UNKNOWN, "Logged in, but no recorded login date.")
    if login_age >= SESSION_STALE_AFTER_DAYS:
        return build(
            HealthState.STALE,
            f"Login is {login_age:.0f}d old (>= {SESSION_STALE_AFTER_DAYS}d) — re-login.",
        )
    if login_age >= SESSION_WARN_AFTER_DAYS:
        return build(HealthState.WARN, f"Login is {login_age:.0f}d old — re-login soon.")
    return build(HealthState.OK, f"Login is {login_age:.0f}d old.")


def _live_identify(profile_name: str) -> tuple[str | None, str | None]:
    """Ask Instagram who this profile is. Returns (username, error_detail).

    Mirrors scripts/instagram_whoami.py but lives here as the canonical
    implementation so the decoupled boundary owns the network specifics.
    """
    try:
        import instaloader
        from instaloader.exceptions import ConnectionException
    except ImportError:
        return None, "instaloader not installed"
    try:
        loader = instaloader.Instaloader(quiet=True, request_timeout=15.0, max_connection_attempts=1)
        if not load_playwright_cookies_into_instaloader(loader, profile_name):
            return None, "no sessionid cookie in saved session"
        username = loader.test_login()
        if not username:
            return None, "Instagram did not return a username (session likely dead)"
        return username, None
    except ConnectionException as exc:
        lowered = str(exc).lower()
        if (
            "401 unauthorized" in lowered
            or "please wait a few minutes" in lowered
            or "graphql/query" in lowered
        ):
            return None, "throttled: Instagram asked us to wait — try again later"
        return None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def live_health(
    profile_name: str,
    account_name: str | None = None,
    *,
    pool: ProfilePool | None = None,
    now: datetime | None = None,
) -> SessionHealth:
    """Authoritative health for one profile via a network 'who am I?' check.

    Starts from :func:`local_health`; if a session exists, confirms it live.
    """
    moment = now or datetime.now(timezone.utc)
    base = local_health(profile_name, account_name, pool=pool, now=moment)
    if base.state == HealthState.NO_SESSION:
        return base  # nothing to confirm

    username, error = _live_identify(profile_name)
    if username:
        return replace(
            base,
            state=HealthState.OK,
            detail=f"Confirmed logged in as @{username}.",
            username=username,
            is_live=True,
        )
    state = HealthState.THROTTLED if (error and "throttled" in error.lower()) else HealthState.LOGGED_OUT
    return replace(base, state=state, detail=error or "Session not confirmed.", is_live=True)
