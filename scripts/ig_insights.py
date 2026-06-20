"""Pull owned-account Instagram reel insights into SQLite and CSV.

This command is read-only against Instagram. It never publishes or mutates
Instagram content.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import pathlib
import sys


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nicheflow_studio.core.env import load_dotenv  # noqa: E402
from nicheflow_studio.core.paths import data_dir  # noqa: E402
from nicheflow_studio.db.post_metrics import upsert_account_post_metrics  # noqa: E402
from nicheflow_studio.services.instagram_insights import (  # noqa: E402
    GraphAPIError,
    collect_account_metrics,
    request_json,
)


CSV_FIELDS = (
    "account_key",
    "shortcode",
    "timestamp",
    "reach",
    "views",
    "total_interactions",
    "saved",
    "shares",
    "likes",
    "comments",
    "conversion_score",
    "caption",
    "pulled_at",
)


def _write_csv(account_key: str, rows: list[dict[str, object]]) -> pathlib.Path:
    output_path = data_dir() / f"ig_insights_{account_key}.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("account_key", help="account env suffix, e.g. pastmomentsdaily")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    account_key = args.account_key.strip().lower()
    env_suffix = account_key.upper()
    token = (os.environ.get(f"IG_TOKEN_{env_suffix}") or "").strip()
    user_id = (os.environ.get(f"IG_USER_ID_{env_suffix}") or "").strip()
    if not token or not user_id:
        parser.error(f"Missing IG_TOKEN_{env_suffix} or IG_USER_ID_{env_suffix} in .env")

    fetch_json = lambda url: request_json(url, token=token)
    try:
        account, rows = collect_account_metrics(
            account_key=account_key,
            user_id=user_id,
            token=token,
            fetch_json=fetch_json,
            pulled_at=dt.datetime.now(dt.timezone.utc),
        )
    except GraphAPIError as exc:
        print(
            f"Instagram Graph request failed (HTTP {exc.status_code}): {exc.body[:600]}",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    upserted = upsert_account_post_metrics(rows)
    csv_path = _write_csv(account_key, rows)
    print(
        f"account=@{account.get('username', '?')} " f"media_count={account.get('media_count', '?')}"
    )
    print(f"upserted={upserted} csv={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
