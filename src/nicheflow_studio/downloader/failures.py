"""Classify download failures shared by the library, queue, and pooling paths.

Two failure classes need opposite handling, so getting this wrong is costly:

* **Source gone** (deleted/private/removed post) — permanent. The clip should be
  pulled out of pools and its assignments released so distribution refills the
  slot with a fresh clip.
* **Auth / rate limit** — transient. The clip is fine; the sourcing session
  needs a re-login or a cool-down. Never remove inventory over this.
"""

from __future__ import annotations

# yt-dlp reports a removed/private/deleted Instagram post with one of these
# phrases. Lowercase for case-insensitive matching. Note that "empty media
# response" can *also* mean an expired session, so UI copy hedges toward
# re-login when many clips fail at once.
SOURCE_GONE_MARKERS = (
    "empty media response",
    "the post may have been deleted",
    "content isn't available",
    "requested content was not found",
    "video unavailable",
    "this account is private",
)

# Instagram refuses a download when the session is dead or the account is being
# throttled. Checked AFTER the gone-source markers.
AUTH_RATE_LIMIT_MARKERS = (
    "rate-limit reached or login required",
    "login required",
    "rate-limit",
    "rate limit",
)


def looks_like_missing_source(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in SOURCE_GONE_MARKERS)


def looks_like_auth_or_rate_limit(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in AUTH_RATE_LIMIT_MARKERS)
