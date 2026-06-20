from __future__ import annotations

from collections.abc import Iterable, Mapping

from nicheflow_studio.db.models import AccountPostMetric
from nicheflow_studio.db.session import get_session


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


def top_titles_for_account(account_key: str, n: int = 5) -> list[str]:
    normalized_key = account_key.strip().lstrip("@").lower()
    if not normalized_key or n <= 0:
        return []
    with get_session() as session:
        rows = (
            session.query(AccountPostMetric.caption)
            .filter(
                AccountPostMetric.account_key == normalized_key,
                AccountPostMetric.caption.is_not(None),
                AccountPostMetric.caption != "",
            )
            .order_by(
                AccountPostMetric.conversion_score.desc(),
                AccountPostMetric.timestamp.desc(),
            )
            .limit(n)
            .all()
        )
    return [caption.strip() for (caption,) in rows if caption and caption.strip()]


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
        session.commit()
    return len(row_list)
