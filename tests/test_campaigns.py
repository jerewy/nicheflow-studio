from __future__ import annotations

import datetime as dt

import pytest

from nicheflow_studio.services import campaigns
from nicheflow_studio.services.campaigns import Campaign, CaptionRuleError

CARDBOUND = Campaign(
    slug="cardbound",
    name="CardBound",
    required_mention="YOUTUBE: CardBound",
    hashtags=("#CardBound", "#Pokemon"),
    min_clip_seconds=7.0,
)


def test_build_caption_appends_mention_and_hashtags() -> None:
    caption = campaigns.build_caption(CARDBOUND, "The card nobody knew existed")

    assert "The card nobody knew existed" in caption
    assert "YOUTUBE: CardBound" in caption
    assert "#CardBound #Pokemon" in caption
    assert not campaigns.validate_caption(CARDBOUND, caption)


def test_build_caption_rejects_indonesian_hook() -> None:
    """The operator writes Indonesian; a slip must fail loudly, not silently ship."""
    with pytest.raises(CaptionRuleError, match="Indonesian words"):
        campaigns.build_caption(CARDBOUND, "Kartu yang tidak ada dengan orang")


def test_build_caption_rejects_non_ascii_characters() -> None:
    with pytest.raises(CaptionRuleError, match="non-English characters"):
        campaigns.build_caption(CARDBOUND, "The rarest card — ever… 日本")


def test_english_hook_with_lookalike_words_passes() -> None:
    """'and'/'be' are English; only the marker list should trip the check."""
    caption = campaigns.build_caption(CARDBOUND, "It sold and nobody could believe the price")

    assert campaigns.check_english_only(caption) == []


def test_validate_caption_flags_a_missing_mention() -> None:
    problems = campaigns.validate_caption(CARDBOUND, "Just a hook with no mention")

    assert len(problems) == 1
    assert "missing required mention" in problems[0]


def test_validate_caption_accepts_a_differently_cased_mention() -> None:
    assert campaigns.validate_caption(CARDBOUND, "hook\n\nyoutube: cardbound") == []


def test_submission_status_counts_down_to_the_one_hour_deadline() -> None:
    posted = dt.datetime(2026, 8, 9, 12, 0, 0)

    status = campaigns.submission_status(posted, posted + dt.timedelta(minutes=15))

    assert status["seconds_remaining"] == 45 * 60
    assert status["expired"] is False
    assert status["view_ceiling"] == 1000


def test_submission_status_expires_after_an_hour() -> None:
    posted = dt.datetime(2026, 8, 9, 12, 0, 0)

    status = campaigns.submission_status(posted, posted + dt.timedelta(minutes=61))

    assert status["seconds_remaining"] == 0
    assert status["expired"] is True
