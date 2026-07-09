"""Register (or update) a publishing account on the Cloudflare Worker.

Reads the account's IG user id from ``.env`` (``IG_USER_ID_<ACCOUNT_KEY>``) and
calls the Worker's ``PUT /v1/accounts`` so it knows how to publish for that
account. The Worker URL + API key also come from ``.env`` (the same vars the app
uses), so nothing sensitive goes on the command line.

The IG **token** is NOT handled here — it must already be a Worker secret named
``IG_TOKEN_<ACCOUNT_KEY>`` (set once with ``wrangler secret put``). This script
only records metadata + safety caps.

By default it first runs a READ-ONLY check against the Graph API using the local
``IG_TOKEN_<ACCOUNT_KEY>`` to confirm the token works and the account is a
Professional (not personal) one — it posts nothing. Use ``--skip-verify`` to
register without the check, or ``--verify-only`` to just run the check.

Examples:
  # Just check the token + account type (posts nothing, registers nothing):
  .venv\\Scripts\\python.exe scripts\\cloudflare_register_account.py beneathhistory --verify-only

  # Verify, then register on the Worker with a conservative cadence for a new account:
  .venv\\Scripts\\python.exe scripts\\cloudflare_register_account.py beneathhistory --daily-limit 3
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from nicheflow_studio.core.env import load_dotenv  # noqa: E402
from nicheflow_studio.services import cloud_publisher  # noqa: E402

# Instagram Login flow (matches the Worker's GRAPH_HOST / GRAPH_VERSION).
GRAPH_HOST = "graph.instagram.com"
GRAPH_VERSION = "v21.0"


def _verify_token(user_id: str, token: str) -> None:
    """Read-only Graph check: confirm the token is valid, points at ``user_id``,
    and the account is Professional. Posts nothing; never prints the token."""
    url = f"https://{GRAPH_HOST}/{GRAPH_VERSION}/me"
    query = urllib.parse.urlencode(
        {"fields": "user_id,username,account_type", "access_token": token}
    )
    request = urllib.request.Request(f"{url}?{query}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # Strip the query string so the token is never echoed.
        raise SystemExit(f"Token check failed (HTTP {exc.code} from {url}):\n{body}")

    username = data.get("username", "?")
    account_type = (data.get("account_type") or "").upper()
    returned_id = str(data.get("user_id") or "")
    print(f"  token OK -> @{username} (account_type={account_type or 'unknown'})")
    if returned_id and returned_id != str(user_id):
        raise SystemExit(
            f"  user id mismatch: .env has {user_id} but the token belongs to {returned_id}."
        )
    if account_type == "PERSONAL":
        raise SystemExit(
            "  This is still a PERSONAL account — the Graph API cannot publish to it. "
            "Convert it to a Professional (Creator) account first."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("account_key", help="e.g. beneathhistory (lowercase Worker key)")
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=4,
        help=(
            "max posts/24h (default 4); set to schedule target + 1 "
            "(headroom lets a delayed queue catch up)"
        ),
    )
    parser.add_argument(
        "--min-gap-minutes", type=int, default=240, help="min minutes between posts (default 240)"
    )
    parser.add_argument(
        "--disabled", action="store_true", help="register but leave the account disabled"
    )
    parser.add_argument("--skip-verify", action="store_true", help="register without the token check")
    parser.add_argument("--verify-only", action="store_true", help="only run the token check")
    args = parser.parse_args()

    load_dotenv()
    prefix = args.account_key.upper()
    user_id = (os.environ.get(f"IG_USER_ID_{prefix}") or "").strip()
    token = (os.environ.get(f"IG_TOKEN_{prefix}") or "").strip()
    token_secret_name = f"IG_TOKEN_{prefix}"

    if not user_id:
        raise SystemExit(f"Missing IG_USER_ID_{prefix} in .env")

    if not args.skip_verify:
        if not token:
            raise SystemExit(f"Missing IG_TOKEN_{prefix} in .env (needed for the read-only check)")
        print(f"Verifying {args.account_key} token (read-only, posts nothing)...")
        _verify_token(user_id, token)

    if args.verify_only:
        print("Verify-only: not registering. Re-run without --verify-only to register.")
        return 0

    if not cloud_publisher.is_configured():
        raise SystemExit(
            "Worker not configured: set CLOUDFLARE_PUBLISHER_URL and "
            "CLOUDFLARE_PUBLISHER_API_KEY in .env."
        )

    print(
        f"Registering '{args.account_key}' on the Worker "
        f"(token_secret_name={token_secret_name}, daily_limit={args.daily_limit}, "
        f"min_gap_minutes={args.min_gap_minutes}, enabled={not args.disabled})..."
    )
    try:
        result = cloud_publisher.upsert_account(
            account_key=args.account_key,
            instagram_user_id=user_id,
            token_secret_name=token_secret_name,
            enabled=not args.disabled,
            daily_limit=args.daily_limit,
            min_gap_minutes=args.min_gap_minutes,
        )
    except cloud_publisher.CloudPublisherError as exc:
        raise SystemExit(f"Registration failed: {exc}")

    print(f"  registered: {json.dumps(result)}")
    print(
        "\nNext: make sure the Worker secret is set "
        f"(wrangler secret put {token_secret_name}), then add "
        f'"<account id>":"{args.account_key}" to CLOUDFLARE_PUBLISH_ACCOUNTS in .env and restart.'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
