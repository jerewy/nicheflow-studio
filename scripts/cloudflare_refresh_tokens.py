"""Refresh the long-lived Instagram tokens behind the Cloudflare publisher.

Instagram Login long-lived tokens expire 60 days after they are issued and CANNOT
be refreshed once expired — so publishing silently dies on a 60-day clock unless
something extends each token first. Refreshing is a server-side call that needs no
re-login: as long as the token is still valid and at least 24 hours old, a single
``GET graph.instagram.com/refresh_access_token`` returns a token good for another
60 days (see Meta's "Refresh Access Token" reference).

This script does that for every cloud-mapped account:

  1. Read the account's current token (from the local refresh cache, or seeded from
     ``.env`` ``IG_TOKEN_<ACCOUNT>`` on first run).
  2. If the token is older than ``--threshold-days`` (default 40, well inside the
     60-day window), call the refresh endpoint.
  3. On success, store the new token + dates in the cache and push it to the Worker
     with ``wrangler secret put IG_TOKEN_<ACCOUNT>``.
  4. On failure (token expired/revoked, account restricted), print a loud per-account
     warning that a MANUAL re-auth is needed for that one — and keep going for the rest.

Accounts come from ``CLOUDFLARE_PUBLISH_ACCOUNTS`` in ``.env`` (the same map the app
uses), so it picks up exactly the accounts that publish via the cloud. The token is
never printed and never put on the command line.

Cache file (gitignored, holds tokens): ``data/cloudflare_token_refresh.json``.

Examples:
  # Show which accounts are due, touch nothing (no network, no secrets written):
  .venv\\Scripts\\python.exe scripts\\cloudflare_refresh_tokens.py --dry-run

  # Refresh every account whose token is older than the threshold:
  .venv\\Scripts\\python.exe scripts\\cloudflare_refresh_tokens.py

  # Force-refresh a single account regardless of age:
  .venv\\Scripts\\python.exe scripts\\cloudflare_refresh_tokens.py --account beneathhistory --force
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from nicheflow_studio.core.env import load_dotenv  # noqa: E402
from nicheflow_studio.services import cloud_publisher  # noqa: E402

# Instagram Login flow — the refresh endpoint is unversioned per Meta's docs.
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
REPO_ROOT = pathlib.Path(__file__).parent.parent
WORKER_DIR = REPO_ROOT / "cloudflare-publisher"
CACHE_PATH = REPO_ROOT / "data" / "cloudflare_token_refresh.json"
# Refresh comfortably before the 60-day expiry so a missed run (or a PC that was off)
# still leaves ~20 days of slack to recover in.
DEFAULT_THRESHOLD_DAYS = 40
# Meta rejects refresh on tokens younger than this; skip rather than burn an API error.
MIN_TOKEN_AGE_HOURS = 24


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def _due_reason(refreshed_at: dt.datetime | None, threshold_days: int, now: dt.datetime) -> str | None:
    """Return why a refresh is due, or None when it can be skipped."""
    if refreshed_at is None:
        return "no recorded refresh date (establishing baseline)"
    age_days = (now - refreshed_at).total_seconds() / 86_400
    if age_days >= threshold_days:
        return f"token is {age_days:.0f}d old (>= {threshold_days}d threshold)"
    return None


def _too_young(refreshed_at: dt.datetime | None, now: dt.datetime) -> bool:
    if refreshed_at is None:
        return False
    return (now - refreshed_at).total_seconds() < MIN_TOKEN_AGE_HOURS * 3600


def _refresh_token(token: str) -> dict:
    """Call the refresh endpoint. Returns the parsed JSON; never echoes the token
    (the access_token lives in the query string, so error text omits the URL)."""
    query = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token})
    request = urllib.request.Request(
        f"{REFRESH_URL}?{query}",
        method="GET",
        headers={"User-Agent": "NicheFlow-Studio-Token-Refresh/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Meta refresh failed (HTTP {exc.code}): {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Meta refresh failed (network): {exc.reason}") from exc


def _push_worker_secret(secret_name: str, value: str) -> None:
    """Set the Worker secret via wrangler, value piped over stdin (never argv)."""
    npx = "npx.cmd" if os.name == "nt" else "npx"
    result = subprocess.run(
        [npx, "wrangler", "secret", "put", secret_name],
        cwd=WORKER_DIR,
        input=value.encode("utf-8"),  # no trailing newline — wrangler keeps the raw bytes
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"wrangler secret put {secret_name} failed: {stderr}")


def _process_account(
    worker_key: str,
    cache: dict[str, dict],
    *,
    threshold_days: int,
    force: bool,
    dry_run: bool,
    now: dt.datetime,
) -> str:
    """Refresh one account if due. Returns a result tag: refreshed/skipped/failed."""
    secret_name = f"IG_TOKEN_{worker_key.upper()}"
    entry = cache.get(worker_key, {})
    refreshed_at = _parse_iso(entry.get("refreshed_at"))
    current_token = entry.get("token") or (os.environ.get(secret_name) or "").strip()

    reason = "forced" if force else _due_reason(refreshed_at, threshold_days, now)
    if not reason:
        next_due = refreshed_at + dt.timedelta(days=threshold_days) if refreshed_at else now
        print(f"  {worker_key}: up to date (next refresh ~{next_due.date().isoformat()})")
        return "skipped"

    if not current_token:
        print(
            f"  [WARN] {worker_key}: due ({reason}) but no token found in cache or "
            f"{secret_name} in .env — seed it once, then re-run."
        )
        return "failed"

    if not force and _too_young(refreshed_at, now):
        print(f"  {worker_key}: skipping — refreshed < {MIN_TOKEN_AGE_HOURS}h ago (Meta rejects that).")
        return "skipped"

    if dry_run:
        print(f"  {worker_key}: WOULD refresh ({reason}).")
        return "skipped"

    print(f"  {worker_key}: refreshing ({reason})...")
    try:
        payload = _refresh_token(current_token)
        new_token = str(payload.get("access_token") or "")
        if not new_token:
            raise RuntimeError(f"no access_token in refresh response: {payload}")
        expires_in = int(payload.get("expires_in") or 0)
        _push_worker_secret(secret_name, new_token)
    except RuntimeError as exc:
        print(
            f"  [WARN] {worker_key}: refresh FAILED — likely expired/revoked/restricted. "
            f"This account needs a MANUAL re-auth (mint a new long-lived token, set "
            f"{secret_name} in .env, then `wrangler secret put {secret_name}`).\n"
            f"         detail: {exc}"
        )
        return "failed"

    expires_at = now + dt.timedelta(seconds=expires_in) if expires_in else None
    cache[worker_key] = {
        "token": new_token,
        "refreshed_at": now.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    _save_cache(cache)  # persist per-account so a later failure can't lose an earlier success
    tail = f", valid until ~{expires_at.date().isoformat()}" if expires_at else ""
    print(f"  {worker_key}: refreshed and pushed to Worker secret {secret_name}{tail}.")
    return "refreshed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", help="only this Worker key (default: all cloud-mapped accounts)")
    parser.add_argument(
        "--threshold-days", type=int, default=DEFAULT_THRESHOLD_DAYS,
        help=f"refresh tokens older than this many days (default {DEFAULT_THRESHOLD_DAYS})",
    )
    parser.add_argument("--force", action="store_true", help="refresh regardless of age")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen; change nothing")
    args = parser.parse_args()

    load_dotenv()
    keys = sorted(set(cloud_publisher.cloud_publish_map().values()))
    if args.account:
        wanted = args.account.strip().lower()
        if wanted not in keys:
            raise SystemExit(
                f"'{wanted}' is not in CLOUDFLARE_PUBLISH_ACCOUNTS (known: {', '.join(keys) or 'none'})."
            )
        keys = [wanted]
    if not keys:
        raise SystemExit("No cloud-mapped accounts. Set CLOUDFLARE_PUBLISH_ACCOUNTS in .env first.")

    if not args.dry_run and not WORKER_DIR.is_dir():
        raise SystemExit(f"Worker directory not found: {WORKER_DIR}")

    now = _now()
    cache = _load_cache()
    print(f"{'[dry-run] ' if args.dry_run else ''}Checking {len(keys)} account(s) (threshold {args.threshold_days}d)...")

    tally = {"refreshed": 0, "skipped": 0, "failed": 0}
    for worker_key in keys:
        tally[_process_account(
            worker_key, cache,
            threshold_days=args.threshold_days, force=args.force, dry_run=args.dry_run, now=now,
        )] += 1

    print(f"\nDone: {tally['refreshed']} refreshed, {tally['skipped']} skipped, {tally['failed']} failed.")
    # Non-zero exit when something needs a human, so a scheduled run can alert.
    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
