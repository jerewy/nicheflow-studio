"""Rotation pool for Instagram login profiles.

Picks the least-recently-used profile that is not cooling down, tracks usage,
and applies cooldowns on rate-limit failures so repeated runs spread load
across multiple accounts.

The manifest lives next to the profile directories at
``data/browser-profiles/instagram/profiles.json`` and is rewritten atomically
on every mutation. Profiles present on disk but missing from the manifest are
auto-registered the first time the pool sees them.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nicheflow_studio.core.instagram_session import (
    DEFAULT_PROFILE_NAME,
    list_profiles,
    profile_root,
    profile_storage_state_path,
)


AUTO_PROFILE = "auto"

# IG sessionids quietly die after ~1-2 weeks of activity. We surface a soft warning
# well before that and refuse to use a profile we know is past the expected lifetime.
SESSION_WARN_AFTER_DAYS = 5
SESSION_STALE_AFTER_DAYS = 14


class NoAvailableProfilesError(RuntimeError):
    """Raised when every profile is either missing or cooling down."""


@dataclass(frozen=True)
class Profile:
    name: str
    last_used: datetime | None = None
    cooldown_until: datetime | None = None
    failure_count: int = 0
    total_requests: int = 0
    last_login: datetime | None = None

    def is_cooled(self, now: datetime) -> bool:
        return self.cooldown_until is None or self.cooldown_until <= now

    def login_age_days(self, now: datetime) -> float | None:
        if self.last_login is None:
            return None
        return (now - self.last_login).total_seconds() / 86_400


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _profile_from_dict(name: str, raw: dict) -> Profile:
    return Profile(
        name=name,
        last_used=_parse_dt(raw.get("last_used")),
        cooldown_until=_parse_dt(raw.get("cooldown_until")),
        failure_count=int(raw.get("failure_count") or 0),
        total_requests=int(raw.get("total_requests") or 0),
        last_login=_parse_dt(raw.get("last_login")),
    )


def _profile_to_dict(profile: Profile) -> dict:
    return {
        "last_used": _format_dt(profile.last_used),
        "cooldown_until": _format_dt(profile.cooldown_until),
        "failure_count": profile.failure_count,
        "total_requests": profile.total_requests,
        "last_login": _format_dt(profile.last_login),
    }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class ProfilePool:
    """Manage rotation across multiple Instagram login profiles.

    The pool is the single source of truth for which profile a scrape run
    should use. Callers ask via :meth:`pick`, do their work, then report back
    with :meth:`mark_used` or :meth:`mark_rate_limited`.
    """

    manifest_path: Path
    profiles: dict[str, Profile] = field(default_factory=dict)

    @classmethod
    def load(cls, manifest_path: Path | None = None) -> "ProfilePool":
        path = manifest_path or (profile_root() / "profiles.json")
        profiles: dict[str, Profile] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                items = raw.get("profiles") if isinstance(raw, dict) else None
                if isinstance(items, dict):
                    for name, data in items.items():
                        if isinstance(name, str) and isinstance(data, dict):
                            profiles[name] = _profile_from_dict(name, data)
            except Exception:  # noqa: BLE001
                profiles = {}
        pool = cls(manifest_path=path, profiles=profiles)
        pool._register_on_disk_profiles()
        return pool

    def _register_on_disk_profiles(self) -> None:
        for name in list_profiles():
            self.profiles.setdefault(name, Profile(name=name))

    def save(self) -> None:
        payload = {
            "profiles": {name: _profile_to_dict(p) for name, p in self.profiles.items()}
        }
        _atomic_write_text(self.manifest_path, json.dumps(payload, indent=2))

    def available(self, *, now: datetime | None = None) -> list[Profile]:
        """Profiles that have cookies on disk, are not cooling down, and whose
        session is not provably stale.

        Stale-session filtering (added with the rate-limit hardening pass)
        prevents the pool from picking a profile whose cookies are >14 days
        old — those would burn a request on a 'login_required' error before
        we discovered they were dead. Callers re-login via
        ``instagram_login_playwright.py`` to bring them back.
        """
        moment = now or datetime.now(timezone.utc)
        return [
            profile
            for profile in self.profiles.values()
            if profile_storage_state_path(profile.name).exists()
            and profile.is_cooled(moment)
            and not self.session_is_stale(profile.name, now=moment)
        ]

    def pick(self, *, now: datetime | None = None) -> Profile:
        """Return the least-recently-used profile that has cookies and is not cooling down.

        Never-used profiles win over any used profile (sorted as epoch zero).
        """
        candidates = self.available(now=now)
        if not candidates:
            cooling = sum(
                1
                for p in self.profiles.values()
                if p.cooldown_until and not p.is_cooled(now or datetime.now(timezone.utc))
            )
            on_disk = len(list_profiles())
            raise NoAvailableProfilesError(
                f"No usable Instagram profile (on-disk: {on_disk}, cooling: {cooling}). "
                "Log in with: python scripts/instagram_login_playwright.py --profile <name>"
            )
        candidates.sort(key=lambda p: (p.last_used or datetime.min.replace(tzinfo=timezone.utc)))
        return candidates[0]

    def _update(self, name: str, **changes) -> Profile:
        current = self.profiles.get(name) or Profile(name=name)
        updated = replace(current, **changes)
        self.profiles[name] = updated
        return updated

    def mark_used(self, name: str, *, count: int = 1, now: datetime | None = None) -> Profile:
        moment = now or datetime.now(timezone.utc)
        current = self.profiles.get(name) or Profile(name=name)
        updated = self._update(
            name,
            last_used=moment,
            total_requests=current.total_requests + max(0, count),
            failure_count=0,
        )
        self.save()
        return updated

    def mark_rate_limited(
        self,
        name: str,
        *,
        hours: float | None = None,
        now: datetime | None = None,
    ) -> Profile:
        """Cool the profile down after a rate-limit signal.

        Graduated cooldown (added with rate-limit hardening): when the caller
        does not pass an explicit ``hours``, we read the prior ``failure_count``
        and pick a duration that escalates with repeated 429s. A one-off 429
        no longer burns the profile for 6 hours — it cools 30 minutes and
        comes back to the rotation, which gives 3-5x more daily throughput
        across the main/alt1/alt2 set.

        Explicit ``hours`` (e.g. from the consecutive-failure-limit branch in
        the scraper) still overrides the graduated default, so callers that
        want a hard cooldown can still ask for one.
        """
        moment = now or datetime.now(timezone.utc)
        current = self.profiles.get(name) or Profile(name=name)
        if hours is None:
            hours = self._graduated_cooldown_hours(current.failure_count)
        updated = self._update(
            name,
            cooldown_until=moment + timedelta(hours=hours),
            failure_count=current.failure_count + 1,
            last_used=moment,
        )
        self.save()
        return updated

    @staticmethod
    def _graduated_cooldown_hours(prior_failure_count: int) -> float:
        """Map prior failure count to a sensible cooldown duration.

        - 0 prior failures: 30 minutes (one-off 429, likely a brief throttle)
        - 1 prior failure:  2 hours (pattern emerging — back off harder)
        - 2+ prior failures: 6 hours (this profile is actively flagged)
        """
        if prior_failure_count <= 0:
            return 0.5
        if prior_failure_count == 1:
            return 2.0
        return 6.0

    def mark_dead(
        self, name: str, *, hours: float = 24.0, now: datetime | None = None
    ) -> Profile:
        moment = now or datetime.now(timezone.utc)
        current = self.profiles.get(name) or Profile(name=name)
        updated = self._update(
            name,
            cooldown_until=moment + timedelta(hours=hours),
            failure_count=current.failure_count + 1,
            last_used=moment,
        )
        self.save()
        return updated

    def mark_login(self, name: str, *, now: datetime | None = None) -> Profile:
        """Record that this profile was just freshly logged in. Clears cooldown + failures."""
        moment = now or datetime.now(timezone.utc)
        updated = self._update(
            name,
            last_login=moment,
            cooldown_until=None,
            failure_count=0,
        )
        self.save()
        return updated

    def session_warning(self, name: str, *, now: datetime | None = None) -> str | None:
        """Return a one-line warning if the profile's login is stale, else None."""
        moment = now or datetime.now(timezone.utc)
        profile = self.profiles.get(name)
        if profile is None:
            return None
        age = profile.login_age_days(moment)
        if age is None:
            return (
                f"profile '{name}' has no recorded login date — "
                f"re-run instagram_login_playwright.py --profile {name} so freshness can be tracked"
            )
        if age >= SESSION_STALE_AFTER_DAYS:
            return (
                f"profile '{name}' login is {age:.1f} days old (>= {SESSION_STALE_AFTER_DAYS}d). "
                f"Session is almost certainly dead — re-run "
                f"instagram_login_playwright.py --profile {name} before scraping."
            )
        if age >= SESSION_WARN_AFTER_DAYS:
            return (
                f"profile '{name}' login is {age:.1f} days old — "
                f"plan to re-run instagram_login_playwright.py --profile {name} soon."
            )
        return None

    def session_is_stale(self, name: str, *, now: datetime | None = None) -> bool:
        profile = self.profiles.get(name)
        if profile is None:
            return False
        age = profile.login_age_days(now or datetime.now(timezone.utc))
        return age is not None and age >= SESSION_STALE_AFTER_DAYS


def resolve_profile_name(requested: str) -> str:
    """Resolve a CLI value to a concrete profile name, picking from the pool when 'auto'."""
    if requested != AUTO_PROFILE:
        return requested
    pool = ProfilePool.load()
    return pool.pick().name
