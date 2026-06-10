"""Local monthly Apify usage tracking for the free-tier reminder.

Apify's ``instagram-scraper`` bills per returned result, and the Free plan gives
roughly 1,850 results/month (see ``scraper/instagram_apify.py``). Reading Apify's
live balance would cost an extra API round-trip and ties us to their account API
shape, so instead we track results consumed locally: each scrape records how many
rows Apify returned, keyed by calendar month. The Scraping tab reads this to warn
before the free tier is exhausted.

This is an approximate guard, not an invoice — it errs toward warning early, so
the user is reminded before risking charges. Stored in the small JSON UI-prefs
file (no DB migration); a missing/corrupt file just reads as zero usage.
"""

from __future__ import annotations

import datetime as dt

from nicheflow_studio.core.ui_prefs import get_ui_pref, set_ui_pref

# Free-plan results/month for apify/instagram-scraper (see that module's header,
# as of 2026-05: ~$5 credit at ~$2.70 per 1,000 results).
APIFY_FREE_MONTHLY_RESULTS = 1850
APIFY_ESTIMATED_USD_PER_1000_RESULTS = 2.70
# Warn once the user has consumed this fraction of the free tier.
APIFY_WARN_FRACTION = 0.8

_USAGE_PREF_KEY = "apify_monthly_results"


def _current_month(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m")


def _usage_map() -> dict[str, int]:
    raw = get_ui_pref(_USAGE_PREF_KEY, {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[str(key)] = int(value)
    return out


def record_apify_results(count: int, *, now: dt.datetime | None = None) -> int:
    """Add ``count`` returned results to this month's tally; return the new total."""
    month = _current_month(now)
    usage = _usage_map()
    if count > 0:
        usage[month] = usage.get(month, 0) + int(count)
        set_ui_pref(_USAGE_PREF_KEY, usage)
    return usage.get(month, 0)


def monthly_apify_usage(*, now: dt.datetime | None = None) -> dict:
    """This month's Apify usage vs the free cap, for the UI reminder."""
    month = _current_month(now)
    used = _usage_map().get(month, 0)
    cap = APIFY_FREE_MONTHLY_RESULTS
    return {
        "month": month,
        "used": used,
        "free_cap": cap,
        "remaining": max(0, cap - used),
        "estimated_cost_usd": round(
            used * APIFY_ESTIMATED_USD_PER_1000_RESULTS / 1000,
            4,
        ),
        "estimated_rate_usd_per_1000": APIFY_ESTIMATED_USD_PER_1000_RESULTS,
        "over_free_tier": used >= cap,
        "warn": used >= int(cap * APIFY_WARN_FRACTION),
    }
