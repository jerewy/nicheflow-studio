"""Classify download failures shared by the library, queue, and pooling paths.

Three failure classes need different handling, so getting this wrong is costly:

* **Source gone** (deleted/private/removed post) — permanent. The clip should be
  pulled out of pools and its assignments released so distribution refills the
  slot with a fresh clip.
* **Auth / rate limit** — transient. The clip is fine; the sourcing session
  needs a re-login or a cool-down. Never remove inventory over this.
* **Offline / DNS** — transient and entirely on our side: the machine couldn't
  reach Instagram at all. The clip AND the session are fine; telling the user to
  check their Instagram login here sends them chasing the wrong fix.
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
    # A removed/private post still returns HTTP 400 on the *logged-in* media-info
    # endpoint (/media/{id}/info/), where an available post returns 200 — so the
    # anonymous "empty media response" above never fires when we fetch with
    # sourcing cookies. Scoped to 400 on purpose: a transient Instagram 5xx on the
    # same endpoint must NOT be mistaken for a gone source (that would retire pooled
    # inventory), and auth/rate-limits surface as 401/403/429, not 400.
    "video info extraction failed: http error 400",
)

# Instagram refuses a download when the session is dead or the account is being
# throttled. Checked AFTER the gone-source markers.
AUTH_RATE_LIMIT_MARKERS = (
    "rate-limit reached or login required",
    "login required",
    "rate-limit",
    "rate limit",
)


# The connection never reached Instagram: DNS lookup failed or the network is
# down. Windows raises getaddrinfo [Errno 11001/11002]; Linux says "Temporary
# failure in name resolution"; macOS "nodename nor servname provided".
OFFLINE_MARKERS = (
    "getaddrinfo failed",
    "failed to resolve",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "no address associated with hostname",
    "network is unreachable",
)


def looks_like_missing_source(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in SOURCE_GONE_MARKERS)


def looks_like_auth_or_rate_limit(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in AUTH_RATE_LIMIT_MARKERS)


def looks_like_offline(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in OFFLINE_MARKERS)
