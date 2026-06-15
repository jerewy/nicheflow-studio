"""Schedule one Cloudflare-to-Meta validation job without publishing it.

This script refuses to continue unless the deployed Worker reports
PUBLISH_MODE=validate. A successful run ends with job status ``validated``:
Cloudflare served the video, Meta created and processed the Reel container, and
the Worker deliberately skipped ``media_publish``.

Required .env values:
  CLOUDFLARE_PUBLISHER_URL=https://nicheflow-publisher.<subdomain>.workers.dev
  CLOUDFLARE_PUBLISHER_API_KEY=<Worker API_KEY secret value>
  <ACCOUNT>_IG_USER_ID=<Instagram user id>

The matching Instagram token must already exist as an encrypted Worker secret,
for example IG_TOKEN_PASTMOMENTSDAILY.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from nicheflow_studio.core.env import load_dotenv  # noqa: E402


def _request(
    url: str,
    *,
    api_key: str | None = None,
    method: str = "GET",
    json_body: dict | None = None,
    file_path: pathlib.Path | None = None,
) -> dict:
    headers = {"User-Agent": "NicheFlow-Studio-Cloudflare-Validator/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")
    elif file_path is not None:
        headers["Content-Type"] = mimetypes.guess_type(file_path.name)[0] or "video/mp4"
        data = file_path.read_bytes()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} from {url}\n{body}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", default="pastmomentsdaily")
    parser.add_argument("--video", required=True)
    parser.add_argument("--caption", default="NicheFlow Cloudflare validation test")
    parser.add_argument("--delay-minutes", type=int, default=3)
    parser.add_argument("--timeout-minutes", type=int, default=20)
    args = parser.parse_args()

    load_dotenv()
    base_url = os.environ.get("CLOUDFLARE_PUBLISHER_URL", "").rstrip("/")
    api_key = os.environ.get("CLOUDFLARE_PUBLISHER_API_KEY", "")
    prefix = args.account.upper()
    instagram_user_id = os.environ.get(f"{prefix}_IG_USER_ID", "")
    if not base_url or not api_key or not instagram_user_id:
        raise SystemExit(
            "Missing CLOUDFLARE_PUBLISHER_URL, CLOUDFLARE_PUBLISHER_API_KEY, "
            f"or {prefix}_IG_USER_ID in .env"
        )

    video_path = pathlib.Path(args.video).resolve()
    if not video_path.is_file():
        raise SystemExit(f"Video not found: {video_path}")
    if video_path.stat().st_size > 100_000_000:
        raise SystemExit("Video exceeds the Cloudflare Free plan's 100 MB request limit.")

    health = _request(f"{base_url}/health")
    if health.get("publish_mode") != "validate":
        raise SystemExit(
            f"Refusing to run: Worker publish_mode is {health.get('publish_mode')!r}, "
            "not 'validate'."
        )

    account_key = args.account.strip().lower()
    token_secret_name = f"IG_TOKEN_{prefix}"
    _request(
        f"{base_url}/v1/accounts",
        api_key=api_key,
        method="PUT",
        json_body={
            "account_key": account_key,
            "instagram_user_id": instagram_user_id,
            "token_secret_name": token_secret_name,
            "enabled": True,
            "daily_limit": 6,
            "min_gap_minutes": 240,
        },
    )

    scheduled_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=args.delay_minutes)
    external_id = f"validate-{account_key}-{uuid.uuid4()}"
    created = _request(
        f"{base_url}/v1/jobs",
        api_key=api_key,
        method="POST",
        json_body={
            "external_id": external_id,
            "account_key": account_key,
            "caption": args.caption,
            "scheduled_at": scheduled_at.isoformat(),
            "file_name": video_path.name,
            "content_type": "video/mp4",
        },
    )
    job_id = created["id"]
    _request(
        f"{base_url}{created['upload_path']}",
        api_key=api_key,
        method="PUT",
        file_path=video_path,
    )
    print(f"Scheduled validation job {job_id} for {scheduled_at.isoformat()}")

    deadline = time.monotonic() + args.timeout_minutes * 60
    while time.monotonic() < deadline:
        jobs = _request(f"{base_url}/v1/jobs", api_key=api_key).get("jobs", [])
        job = next((row for row in jobs if row.get("id") == job_id), None)
        if not job:
            raise SystemExit("Validation job disappeared from the Worker.")
        status = job.get("status")
        print(f"status: {status}")
        if status == "validated":
            print("[ok] Cloudflare-to-Meta scheduled validation finished. Nothing was posted.")
            return 0
        if status in {"failed", "canceled", "published"}:
            raise SystemExit(f"Validation ended unexpectedly: {job}")
        time.sleep(20)

    raise SystemExit("Timed out waiting for the validation job.")


if __name__ == "__main__":
    raise SystemExit(main())
