from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping

from nicheflow_studio.db.models import Account, AccountPostMetric, UploadJob
from nicheflow_studio.db.session import get_session


# conversion_score is interactions/reach, so a post Instagram barely distributed
# scores on a tiny denominator: a real account's WORST post (163 views, reach 75)
# ranked second among its "winners" purely because a handful of likes over reach
# 75 beats 900k engaged over reach 912k. Few-shot examples teach register, so
# feeding that back would train the model on the posts that failed. Require a
# post to have actually been distributed before its wording counts as a win.
MIN_WINNER_REACH = 1_000

# posted_at (when the app handed the reel to Instagram) and the media's own
# timestamp differ by the API round-trip — measured at 1-3s across a real
# account. Posts sit hours apart, so a minute of slack pins each metric row to
# exactly one upload job with no ambiguity.
_POSTED_AT_TOLERANCE = dt.timedelta(seconds=120)


_MUTABLE_FIELDS = (
    "caption",
    "timestamp",
    "reach",
    "views",
    "likes",
    "comments",
    "saved",
    "shares",
    "total_interactions",
    "conversion_score",
    "pulled_at",
)


def _first_sentence(text: str) -> str:
    """The caption's opening hook, used when no upload job matches.

    A full Instagram caption is 80-150 words; the slot these feed is a list of
    one-line title examples. Passing the whole caption teaches length, not
    register. The opening line is the hook and is the closest stand-in.
    """
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    head, sep, _ = first_line.partition(". ")
    return f"{head}." if sep else first_line


def top_titles_for_account(account_key: str, n: int = 5) -> list[str]:
    """This account's best-performing ON-SCREEN TITLES, strongest first.

    Returns the title the app actually applied and posted (``UploadJob.title``),
    not the Instagram caption: these are few-shot examples for the title rules,
    and the two are different texts of very different lengths. Metric rows carry
    no job id, so they are matched on posting time (see _POSTED_AT_TOLERANCE);
    a row with no match falls back to its caption's opening line rather than
    dropping a genuine winner.
    """
    normalized_key = account_key.strip().lstrip("@").lower()
    if not normalized_key or n <= 0:
        return []
    with get_session() as session:
        rows = (
            session.query(AccountPostMetric.caption, AccountPostMetric.timestamp)
            .filter(
                AccountPostMetric.account_key == normalized_key,
                AccountPostMetric.caption.is_not(None),
                AccountPostMetric.caption != "",
                # Reach can be NULL on rows pulled before insights were available.
                AccountPostMetric.reach.is_not(None),
                AccountPostMetric.reach >= MIN_WINNER_REACH,
            )
            .order_by(
                AccountPostMetric.conversion_score.desc(),
                AccountPostMetric.timestamp.desc(),
            )
            .limit(n)
            .all()
        )
        if not rows:
            return []
        account_id = (
            session.query(Account.id)
            .filter(Account.instagram_handle == normalized_key)
            .scalar()
        )
        jobs: list[tuple[dt.datetime, str]] = []
        if account_id is not None:
            jobs = [
                (posted_at, title)
                for posted_at, title in session.query(UploadJob.posted_at, UploadJob.title)
                .filter(
                    UploadJob.account_id == account_id,
                    UploadJob.posted_at.is_not(None),
                    UploadJob.title.is_not(None),
                    UploadJob.title != "",
                )
                .all()
                if posted_at is not None
            ]

    winners: list[str] = []
    for caption, timestamp in rows:
        title = _title_posted_at(jobs, timestamp)
        winner = (title or _first_sentence(caption or "")).strip()
        if winner:
            winners.append(winner)
    return winners


def _title_posted_at(jobs: list[tuple[dt.datetime, str]], timestamp) -> str | None:
    """The applied title of the upload job posted at ``timestamp``, if any."""
    if timestamp is None:
        return None
    best: tuple[dt.timedelta, str] | None = None
    for posted_at, title in jobs:
        # Metric timestamps carry a UTC offset; posted_at is stored naive.
        moment = timestamp.replace(tzinfo=None) if timestamp.tzinfo else timestamp
        delta = abs(moment - posted_at)
        if delta <= _POSTED_AT_TOLERANCE and (best is None or delta < best[0]):
            best = (delta, title)
    return best[1] if best else None


def upsert_account_post_metrics(rows: Iterable[Mapping[str, object]]) -> int:
    row_list = list(rows)
    with get_session() as session:
        for row in row_list:
            account_key = str(row["account_key"])
            shortcode = str(row["shortcode"])
            metric = (
                session.query(AccountPostMetric)
                .filter(
                    AccountPostMetric.account_key == account_key,
                    AccountPostMetric.shortcode == shortcode,
                )
                .one_or_none()
            )
            if metric is None:
                metric = AccountPostMetric(account_key=account_key, shortcode=shortcode)
                session.add(metric)
            for field in _MUTABLE_FIELDS:
                setattr(metric, field, row[field])
            # Set apart from _MUTABLE_FIELDS (which indexes rows directly and so
            # requires every key): callers built before this column exists still
            # pass valid rows, and must not blank an id an earlier pull stored.
            media_id = row.get("media_id")
            if media_id:
                metric.media_id = str(media_id)
        session.commit()
    return len(row_list)
