"""Remembered UI preferences, stored in the app database.

The webview cannot hold these: pywebview defaults to private mode and serves the
built UI on a port that changes every launch, and localStorage is partitioned by
origin — so anything the UI remembered browser-side was silently discarded on
restart. Keeping preferences here makes them independent of webview storage and,
unlike localStorage, testable.

Values are JSON so a preference can be a scalar, a list, or an object. Unknown
keys read back as the caller's default rather than raising: a preference that
has never been set is a normal state, not an error.
"""

from __future__ import annotations

import json
import logging

from nicheflow_studio.db.models import UiSetting
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

logger = logging.getLogger(__name__)

# Guards against a typo'd key silently filling the table with junk, and caps a
# runaway value before it hits the column limit.
_MAX_KEY_LENGTH = 128
_MAX_VALUE_LENGTH = 8192


class UiSettingError(ServiceError):
    """Raised for an unusable key or a value that cannot be stored."""


def _clean_key(key: str) -> str:
    cleaned = (key or "").strip()
    if not cleaned:
        raise UiSettingError("A UI setting key is required.")
    if len(cleaned) > _MAX_KEY_LENGTH:
        raise UiSettingError(f"UI setting key is longer than {_MAX_KEY_LENGTH} characters.")
    return cleaned


def get_setting(key: str, default=None):
    """The stored value for ``key``, or ``default`` when unset or unreadable.

    A corrupt row returns the default instead of raising: a preference is never
    worth breaking a screen over.
    """
    cleaned = _clean_key(key)
    with get_session() as session:
        row = session.get(UiSetting, cleaned)
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except (TypeError, ValueError):
            logger.warning("UI setting %s holds unreadable JSON; using the default.", cleaned)
            return default


def set_setting(key: str, value) -> dict:
    """Store ``value`` (JSON-serialisable) for ``key``. Upserts."""
    cleaned = _clean_key(key)
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise UiSettingError(f"UI setting {cleaned!r} is not JSON-serialisable: {exc}") from exc
    if len(encoded) > _MAX_VALUE_LENGTH:
        raise UiSettingError(
            f"UI setting {cleaned!r} is {len(encoded)} characters, over the "
            f"{_MAX_VALUE_LENGTH} limit."
        )
    with get_session() as session:
        row = session.get(UiSetting, cleaned)
        if row is None:
            session.add(UiSetting(key=cleaned, value=encoded))
        else:
            row.value = encoded
        session.commit()
    return {"key": cleaned, "value": value}


def get_settings(keys: list[str]) -> dict:
    """Several settings in one round trip.

    The batch screen restores three preferences at once; fetching them
    individually would make the first paint depend on three bridge calls.
    """
    return {key: get_setting(key) for key in keys}
