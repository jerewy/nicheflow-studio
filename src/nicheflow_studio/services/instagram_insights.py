from __future__ import annotations

from collections.abc import Callable, Iterator
import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request


GRAPH_HOST = "graph.instagram.com"
GRAPH_VERSION = "v21.0"
INSIGHT_METRIC_SETS = (
    "reach,likes,comments,saved,shares,total_interactions,views",
    "reach,likes,comments,saved,shares,total_interactions",
    "reach,likes,comments,saved",
)


# Graph error subcodes meaning "this media can never have insights", as opposed
# to a transient failure worth retrying or aborting on:
#   2108006 - the post predates the account's conversion from personal to
#             business/creator, so Instagram never recorded insights for it.
_UNAVAILABLE_SUBCODES = ("2108006",)


def _is_permanently_unavailable(body: str) -> bool:
    return any(subcode in (body or "") for subcode in _UNAVAILABLE_SUBCODES)


class GraphAPIError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Graph API request failed with HTTP {status_code}")


def request_json(
    url: str,
    *,
    token: str,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NicheFlow-Insights/1.0"},
        method="GET",
    )
    try:
        with opener(request, timeout=30) as response:  # type: ignore[attr-defined]
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        redacted = body.replace(token, "<redacted>") if token else body
        raise GraphAPIError(exc.code, redacted) from None
    except urllib.error.URLError as exc:
        body = str(exc.reason)
        redacted = body.replace(token, "<redacted>") if token else body
        raise GraphAPIError(0, redacted) from None
    if not isinstance(payload, dict):
        raise GraphAPIError(0, "Graph API returned a non-object response")
    return payload


def collect_account_metrics(
    *,
    account_key: str,
    user_id: str,
    token: str,
    fetch_json: Callable[[str], dict[str, object]],
    pulled_at: dt.datetime,
    on_page: Callable[[list[dict[str, object]]], None] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Pull every post's insights for one owned account.

    Costs one request per post, and Graph API quotas are per-user-per-hour, so a
    few-hundred-post account can be throttled part way through. ``on_page`` is
    called with each page's rows as they are built, letting the caller persist
    incrementally: a throttle then costs the current page instead of the whole
    run. Without it a 300-post account that failed at post 290 saved nothing.
    """
    me_query = urllib.parse.urlencode(
        {
            "fields": "user_id,username,account_type,media_count",
            "access_token": token,
        }
    )
    account = fetch_json(f"https://{GRAPH_HOST}/{GRAPH_VERSION}/me?{me_query}")
    returned_user_id = str(account.get("user_id") or "")
    if returned_user_id and returned_user_id != user_id:
        raise ValueError(
            f"Configured user id {user_id} does not match token user id {returned_user_id}"
        )

    rows: list[dict[str, object]] = []
    skipped = 0
    for page in iter_account_media_pages(
        user_id=user_id,
        token=token,
        fetch_json=fetch_json,
    ):
        page_rows: list[dict[str, object]] = []
        for media in page:
            media_id = str(media.get("id") or "")
            if not media_id:
                continue
            insights = fetch_media_insights(
                media_id=media_id,
                token=token,
                fetch_json=fetch_json,
            )
            if insights is None:
                # Predates the business-account conversion; no insights exist and
                # never will. Skipped rather than stored as zeros.
                skipped += 1
                continue
            page_rows.append(
                build_metric_row(
                    account_key=account_key,
                    media=media,
                    insights=insights,
                    pulled_at=pulled_at,
                )
            )
        rows.extend(page_rows)
        if on_page is not None and page_rows:
            on_page(page_rows)
    account = {**account, "insights_unavailable": skipped}
    return account, rows


def extract_shortcode(permalink: str) -> str:
    parts = [part for part in urllib.parse.urlsplit(permalink).path.split("/") if part]
    for marker in ("reel", "p"):
        if marker in parts:
            marker_index = parts.index(marker)
            if marker_index + 1 < len(parts):
                return parts[marker_index + 1]
    raise ValueError(f"Instagram permalink does not contain a shortcode: {permalink}")


def iter_account_media_pages(
    *,
    user_id: str,
    token: str,
    fetch_json: Callable[[str], dict[str, object]],
) -> Iterator[list[dict[str, object]]]:
    """Yield the account's media one API page at a time, following ``paging.next``.

    Paged rather than flattened so callers can checkpoint per page (see
    ``collect_account_metrics``).
    """
    query = urllib.parse.urlencode(
        {
            "fields": "id,caption,media_type,media_product_type,permalink,timestamp",
            "limit": 50,
            "access_token": token,
        }
    )
    url: str | None = f"https://{GRAPH_HOST}/{GRAPH_VERSION}/{user_id}/media?{query}"
    while url:
        payload = fetch_json(url)
        page = payload.get("data")
        if isinstance(page, list):
            yield [item for item in page if isinstance(item, dict)]
        paging = payload.get("paging")
        next_url = paging.get("next") if isinstance(paging, dict) else None
        url = next_url if isinstance(next_url, str) and next_url else None


def fetch_account_media(
    *,
    user_id: str,
    token: str,
    fetch_json: Callable[[str], dict[str, object]],
) -> list[dict[str, object]]:
    """Every media item for the account, flattened across pages."""
    return [
        item
        for page in iter_account_media_pages(
            user_id=user_id, token=token, fetch_json=fetch_json
        )
        for item in page
    ]


def build_metric_row(
    *,
    account_key: str,
    media: dict[str, object],
    insights: dict[str, int],
    pulled_at: dt.datetime,
) -> dict[str, object]:
    permalink = str(media.get("permalink") or "")
    timestamp_value = media.get("timestamp")
    timestamp = None
    if isinstance(timestamp_value, str) and timestamp_value:
        timestamp = dt.datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))

    normalized = {
        name: int(insights.get(name, 0))
        for name in (
            "reach",
            "views",
            "likes",
            "comments",
            "saved",
            "shares",
            "total_interactions",
        )
    }
    return {
        "account_key": account_key,
        "shortcode": extract_shortcode(permalink),
        "media_id": str(media.get("id") or "") or None,
        "caption": str(media.get("caption") or ""),
        "timestamp": timestamp,
        **normalized,
        "conversion_score": calculate_conversion_score(
            reach=normalized["reach"],
            likes=normalized["likes"],
            comments=normalized["comments"],
            saved=normalized["saved"],
            shares=normalized["shares"],
        ),
        "pulled_at": pulled_at,
    }


def calculate_conversion_score(
    *, reach: int, likes: int, comments: int, saved: int, shares: int
) -> float:
    return (3 * saved + 3 * shares + 2 * comments + likes) / max(reach, 1)


def fetch_media_insights(
    *,
    media_id: str,
    token: str,
    fetch_json: Callable[[str], dict[str, object]],
) -> dict[str, int] | None:
    """This media's insights, or None when Instagram will never provide them.

    Returning None (rather than raising) matters for accounts that predate their
    own conversion to a Business/Creator profile: Instagram refuses insights for
    that older media permanently, so one such post would otherwise abort the
    whole account's pull. The caller skips those posts instead — storing zeros
    would make an unmeasurable post look like a flop in the analysis.
    """
    for index, metrics in enumerate(INSIGHT_METRIC_SETS):
        query = urllib.parse.urlencode({"metric": metrics, "access_token": token})
        url = f"https://{GRAPH_HOST}/{GRAPH_VERSION}/{media_id}/insights?{query}"
        try:
            payload = fetch_json(url)
        except GraphAPIError as exc:
            if exc.status_code == 400 and _is_permanently_unavailable(exc.body):
                return None
            if exc.status_code == 400 and index < len(INSIGHT_METRIC_SETS) - 1:
                continue
            raise

        parsed: dict[str, int] = {}
        data = payload.get("data")
        if not isinstance(data, list):
            return parsed
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            values = item.get("values")
            if not isinstance(name, str) or not isinstance(values, list) or not values:
                continue
            first_value = values[0]
            if not isinstance(first_value, dict):
                continue
            value = first_value.get("value")
            if isinstance(value, (int, float)):
                parsed[name] = int(value)
        return parsed

    return {}
