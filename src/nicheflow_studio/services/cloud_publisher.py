"""Client for the deployed Cloudflare publishing Worker.

Thin HTTP wrapper over the Worker's ``/v1`` API (see ``cloudflare-publisher/``).
The Worker owns scheduling, R2 media hosting, and the Meta Graph API calls; this
module just lets NicheFlow Studio enqueue a reel and read job status, so the
local app never holds Instagram tokens or posts directly.

Configuration (both required; absent => :func:`is_configured` is False and the
caller falls back to the local publisher):

- ``CLOUDFLARE_PUBLISHER_URL``     e.g. https://nicheflow-publisher.<sub>.workers.dev
- ``CLOUDFLARE_PUBLISHER_API_KEY`` the Worker's ``API_KEY`` secret (Bearer auth)

Uses urllib (no new dependency, matching the rest of the codebase). Failures
raise :class:`CloudPublisherError` with the Worker's error body so the UI can
surface a handled message.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from nicheflow_studio.services.errors import ServiceError

_URL_ENV = "CLOUDFLARE_PUBLISHER_URL"
_KEY_ENV = "CLOUDFLARE_PUBLISHER_API_KEY"
# Opt-in map of local account id (string) -> Worker account_key, as JSON, e.g.
# CLOUDFLARE_PUBLISH_ACCOUNTS={"7":"pastmomentsdaily"}. Empty/unset => no account
# publishes via the cloud (the local Playwright path is used), so this whole
# feature is inert until an account is explicitly mapped.
_ACCOUNTS_ENV = "CLOUDFLARE_PUBLISH_ACCOUNTS"
_TIMEOUT_S = 120


def cloud_publish_map() -> dict[str, str]:
    """Parsed account-id -> Worker-key map from env (empty on unset/invalid)."""
    raw = (os.environ.get(_ACCOUNTS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if str(value).strip()}


def cloud_account_key_for(account_id: int) -> str | None:
    """Worker account_key for a local account id, or None if not cloud-mapped."""
    return cloud_publish_map().get(str(account_id))


class CloudPublisherError(ServiceError):
    """Raised for cloud-publisher configuration, transport, or API errors."""


def is_configured() -> bool:
    """True when both the Worker URL and API key are set."""
    return bool((os.environ.get(_URL_ENV) or "").strip() and (os.environ.get(_KEY_ENV) or "").strip())


def _base_url() -> str:
    url = (os.environ.get(_URL_ENV) or "").strip().rstrip("/")
    if not url:
        raise CloudPublisherError(f"{_URL_ENV} is not set")
    return url


def _api_key() -> str:
    key = (os.environ.get(_KEY_ENV) or "").strip()
    if not key:
        raise CloudPublisherError(f"{_KEY_ENV} is not set")
    return key


def _request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
) -> dict:
    # A named User-Agent is required: Cloudflare's edge blocks the default
    # "Python-urllib/*" signature with a 403 (error code 1010) before the
    # request ever reaches the Worker.
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "User-Agent": "NicheFlow-Studio/1.0",
    }
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        headers["Content-Type"] = content_type or "application/octet-stream"
    request = urllib.request.Request(_base_url() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CloudPublisherError(f"Cloud publisher HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudPublisherError(f"Cloud publisher unreachable: {exc.reason}") from exc


def create_job(
    *,
    external_id: str,
    account_key: str,
    caption: str,
    scheduled_at: str,
    file_name: str,
    content_type: str = "video/mp4",
) -> dict:
    """Create a publish job (status ``awaiting_upload``). Returns ``upload_path``."""
    return _request(
        "POST",
        "/v1/jobs",
        json_body={
            "external_id": external_id,
            "account_key": account_key,
            "caption": caption or "",
            "scheduled_at": scheduled_at,
            "file_name": file_name,
            "content_type": content_type,
        },
    )


def upload_media(upload_path: str, video_path: str | Path, *, content_type: str = "video/mp4") -> dict:
    """Stream the MP4 to the job's R2 upload path; flips the job to ``scheduled``."""
    return _request("PUT", upload_path, raw_body=Path(video_path).read_bytes(), content_type=content_type)


def schedule_reel(
    *,
    external_id: str,
    account_key: str,
    caption: str,
    scheduled_at: str,
    video_path: str | Path,
    content_type: str = "video/mp4",
) -> dict:
    """Create the job then upload the video in one call. Returns the created job."""
    path = Path(video_path)
    if not path.is_file():
        raise CloudPublisherError(f"video not found: {path}")
    created = create_job(
        external_id=external_id,
        account_key=account_key,
        caption=caption,
        scheduled_at=scheduled_at,
        file_name=path.name,
        content_type=content_type,
    )
    upload_path = created.get("upload_path")
    if not upload_path:
        raise CloudPublisherError(f"Worker returned no upload_path: {created}")
    upload_media(upload_path, path, content_type=content_type)
    return created


def upsert_account(
    *,
    account_key: str,
    instagram_user_id: str,
    token_secret_name: str,
    enabled: bool = True,
    daily_limit: int = 6,
    min_gap_minutes: int = 240,
) -> dict:
    """Register or update a publishing account on the Worker (PUT ``/v1/accounts``).

    The IG access token is **never** sent here — it lives only as a Worker secret
    named ``token_secret_name`` (set with ``wrangler secret put``). This call just
    records the metadata the Worker needs to find that secret and enforce its
    per-account caps (``daily_limit``, ``min_gap_minutes``).
    """
    return _request(
        "PUT",
        "/v1/accounts",
        json_body={
            "account_key": account_key,
            "instagram_user_id": instagram_user_id,
            "token_secret_name": token_secret_name,
            "enabled": enabled,
            "daily_limit": daily_limit,
            "min_gap_minutes": min_gap_minutes,
        },
    )


def list_jobs() -> dict:
    """All publish jobs known to the Worker (and the current publish_mode)."""
    return _request("GET", "/v1/jobs")


def get_usage() -> dict:
    """Worker storage/active-job usage against the free-tier safety caps."""
    return _request("GET", "/v1/usage")


def cancel_job(job_id: str) -> dict:
    """Cancel a job and delete its R2 media (if not already published/validated)."""
    return _request("POST", f"/v1/jobs/{job_id}/cancel")


def run_due() -> dict:
    """Ask the Worker to process due jobs immediately (POST ``/v1/run``).

    The Worker also runs this on a 1-minute cron; calling it directly lets a cloud
    "Publish Now" start the Meta container right away instead of waiting up to a
    minute for the next cron tick. Returns the Worker's ``{processed, mode}``.
    """
    return _request("POST", "/v1/run")
