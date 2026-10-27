"""Integration keys the user enters on the Settings screen.

Keys are stored in the per-user settings file (``core.paths.settings_env_path``)
in the same KEY=value format as ``.env``, so the file stays hand-editable. See
``core.env.load_app_env`` for the startup precedence: real environment
variables, then this file, then the repo ``.env``.

Saving also applies the value to ``os.environ`` straight away. Apify, the cloud
publisher, and Groq read ``os.environ`` on every call, so a saved key works
without a restart.

Secret values never travel back to the UI. Status reports whether a key is set,
where it came from, and its last four characters.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.env import preexisting_env_keys, read_env_file
from nicheflow_studio.core.paths import settings_env_path
from nicheflow_studio.services import cloud_publisher
from nicheflow_studio.services.errors import ServiceError


class AppSettingsError(ServiceError):
    """Raised for an unknown setting or a value that fails validation."""


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    group: str
    secret: bool
    help: str
    # "text" | "url" | "account_map" | "handles"; drives validation and the input.
    kind: str = "text"


FIELDS: tuple[SettingField, ...] = (
    SettingField(
        key="APIFY_TOKEN",
        label="Apify API token",
        group="apify",
        secret=True,
        help="Scrapes Instagram source profiles. Find it at console.apify.com > Settings > API & Integrations.",
    ),
    SettingField(
        key="CLOUDFLARE_PUBLISHER_URL",
        label="Worker URL",
        group="cloudflare",
        secret=False,
        kind="url",
        help="Your deployed publisher Worker, for example https://nicheflow-publisher.<your-subdomain>.workers.dev.",
    ),
    SettingField(
        key="CLOUDFLARE_PUBLISHER_API_KEY",
        label="Worker API key",
        group="cloudflare",
        secret=True,
        help="The value you gave `npx wrangler secret put API_KEY` when deploying the Worker.",
    ),
    SettingField(
        key="CLOUDFLARE_PUBLISH_ACCOUNTS",
        label="Cloud-published accounts",
        group="cloudflare",
        secret=False,
        kind="account_map",
        help=(
            "Which local accounts publish through the Worker, and the account_key "
            "each one is registered under there."
        ),
    ),
    SettingField(
        key="GROQ_API_KEY",
        label="Groq API key",
        group="ai",
        secret=True,
        help=(
            "Optional. Enables Generate with Groq and the frame-vision pass. "
            "Without it, use the copy-prompt chat path."
        ),
    ),
    SettingField(
        key="NICHEFLOW_IG_SOURCING_PROFILES",
        label="Sourcing profiles",
        group="instagram",
        secret=False,
        kind="handles",
        help=(
            "Comma-separated logged-in Instagram profiles allowed to authenticate "
            "downloads. Never list an account you publish from."
        ),
    ),
)

_FIELDS_BY_KEY = {field.key: field for field in FIELDS}
_CHECKABLE_SERVICES = ("apify", "cloudflare")
# Instagram handles: letters, digits, dot, underscore.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")
# Mirrors normalizeAccountKey in cloudflare-publisher/src/helpers.ts, so a key
# accepted here is never rejected by the Worker later.
_WORKER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
_LOCAL_HOSTS = ("localhost", "127.0.0.1")
_MASK_MIN_LENGTH = 12


def _mask(value: str) -> str:
    # Short values would give most of the secret away, so they get no hint.
    if len(value) < _MASK_MIN_LENGTH:
        return "set"
    return f"…{value[-4:]}"


def _source_for(key: str, current: str, app_values: dict[str, str], repo_values: dict[str, str]) -> str | None:
    if not current:
        return None
    if key in preexisting_env_keys():
        return "system"
    if app_values.get(key) == current:
        return "app"
    if repo_values.get(key) == current:
        return "env_file"
    return "system"


def settings_status() -> dict:
    """Every known field with its state. Secret values are masked."""
    app_values = read_env_file(settings_env_path())
    repo_values = read_env_file(Path.cwd() / ".env")
    fields = []
    for field in FIELDS:
        current = (os.environ.get(field.key) or "").strip()
        fields.append(
            {
                "key": field.key,
                "label": field.label,
                "group": field.group,
                "secret": field.secret,
                "kind": field.kind,
                "help": field.help,
                "is_set": bool(current),
                "source": _source_for(field.key, current, app_values, repo_values),
                "value": None if field.secret else current,
                "hint": _mask(current) if field.secret and current else None,
            }
        )
    return {"settings_path": str(settings_env_path()), "fields": fields}


def _validate_url(value: str) -> str:
    match = re.match(r"^(https?)://([^/:]+)", value)
    if not match:
        raise AppSettingsError("Worker URL must start with https://.")
    scheme, host = match.groups()
    # Plain http is only acceptable for `wrangler dev` on this machine.
    if scheme == "http" and host not in _LOCAL_HOSTS:
        raise AppSettingsError("Worker URL must use https:// (http is only allowed for localhost).")
    return value.rstrip("/")


def _validate_account_map(value: str) -> str:
    try:
        data = json.loads(value)
    except ValueError as exc:
        raise AppSettingsError("Cloud-published accounts must be a JSON object.") from exc
    if not isinstance(data, dict):
        raise AppSettingsError("Cloud-published accounts must be a JSON object.")
    cleaned: dict[str, str] = {}
    for local_id, worker_key in data.items():
        if not str(local_id).isdigit():
            raise AppSettingsError(f"Account id {local_id!r} must be a number.")
        key = str(worker_key or "").strip().lower()
        if not key:
            continue
        if not _WORKER_KEY_RE.match(key):
            raise AppSettingsError(
                f"Worker account key {key!r} must be 2-63 lowercase letters, numbers, "
                "underscores, or hyphens."
            )
        cleaned[str(int(local_id))] = key
    return json.dumps(cleaned, sort_keys=True) if cleaned else ""


def _validate_handles(value: str) -> str:
    handles = [part.strip().lstrip("@") for part in value.split(",") if part.strip()]
    for handle in handles:
        if not _HANDLE_RE.match(handle):
            raise AppSettingsError(
                f"Profile {handle!r} may only use letters, digits, dot and underscore."
            )
    return ",".join(handles)


def _validate(field: SettingField, value: str) -> str:
    if field.kind == "url":
        return _validate_url(value)
    if field.kind == "account_map":
        return _validate_account_map(value)
    if field.kind == "handles":
        return _validate_handles(value)
    if any(char.isspace() for char in value):
        raise AppSettingsError(f"{field.label} cannot contain spaces.")
    return value


def _clean_values(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        raise AppSettingsError("Settings must be sent as an object of key/value pairs.")
    cleaned: dict[str, str] = {}
    for key, raw in values.items():
        field = _FIELDS_BY_KEY.get(key)
        if field is None:
            raise AppSettingsError(f"Unknown setting {key!r}.")
        value = "" if raw is None else str(raw).strip()
        if "\n" in value or "\r" in value:
            raise AppSettingsError(f"{field.label} must be a single line.")
        cleaned[key] = _validate(field, value) if value else ""
    return cleaned


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    # Owner-only modes matter on POSIX. Windows ignores them; there the file is
    # protected by the per-user ACL on %LOCALAPPDATA% (packaged builds).
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = [
        "# NicheFlow Studio settings, written by the Settings screen.",
        "# Same format as .env. Real environment variables take precedence.",
        *(f"{key}={values[key]}" for key in sorted(values)),
    ]
    # Write then swap, so a crash mid-write cannot leave a half-written file
    # that silently drops every key after the cut.
    temp_path = path.with_suffix(".tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temp_path, path)


def _apply_to_environment(key: str, value: str) -> None:
    if key in preexisting_env_keys():
        # A real environment variable still wins; the saved value applies once
        # that variable is removed.
        return
    if value:
        os.environ[key] = value
        return
    os.environ.pop(key, None)
    fallback = read_env_file(Path.cwd() / ".env").get(key)
    if fallback:
        os.environ[key] = fallback


def save_settings(values: object) -> dict:
    """Store the given fields. An empty value removes the key from the file.

    Fields left out of ``values`` are untouched, so the UI can send only what
    the user changed (a secret input left blank means "keep the current key").
    """
    cleaned = _clean_values(values)
    path = settings_env_path()
    stored = read_env_file(path)
    for key, value in cleaned.items():
        if value:
            stored[key] = value
        else:
            stored.pop(key, None)
    _write_env_file(path, stored)
    for key, value in cleaned.items():
        _apply_to_environment(key, value)
    return settings_status()


def _test_apify() -> str:
    token = (os.environ.get("APIFY_TOKEN") or "").strip()
    if not token:
        raise AppSettingsError("Add an Apify token first.")
    from apify_client import ApifyClient
    from apify_client._errors import ApifyApiError

    try:
        user = ApifyClient(token).user().get() or {}
    except ApifyApiError as exc:
        raise AppSettingsError(f"Apify rejected the token: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - network failures should read as such
        raise AppSettingsError(f"Could not reach Apify: {exc}") from exc
    username = user.get("username") or "your account"
    return f"Connected to Apify as {username}."


def _test_cloudflare() -> str:
    if not cloud_publisher.is_configured():
        raise AppSettingsError("Add the Worker URL and API key first.")
    usage = cloud_publisher.get_usage()
    active = usage.get("active_jobs")
    suffix = f" {active} active job(s)." if isinstance(active, int) else ""
    return f"Worker reachable and the API key was accepted.{suffix}"


def check_connection(service: str) -> dict:
    """Check that the saved keys for ``service`` work.

    Returns ``{"ok": bool, "message": str}`` instead of raising, so the screen
    can show a failure next to the button rather than as a generic error.
    """
    if service not in _CHECKABLE_SERVICES:
        raise AppSettingsError(f"No connection check for {service!r}.")
    try:
        message = _test_apify() if service == "apify" else _test_cloudflare()
    except ServiceError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": message}
