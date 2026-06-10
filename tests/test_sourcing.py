from __future__ import annotations

import pytest
from sqlalchemy import select

from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import sourcing
from nicheflow_studio.services.sourcing import SourcingError


def _make_account(name: str = "Past Moments", platform: str = "instagram") -> int:
    with get_session() as session:
        account = Account(name=name, platform=platform)
        session.add(account)
        session.commit()
        return account.id


def test_add_source_infers_type_and_label() -> None:
    account_id = _make_account()

    profile = sourcing.add_source(account_id, "https://www.instagram.com/thehistologian/")
    tag = sourcing.add_source(account_id, "https://www.instagram.com/explore/tags/history")

    assert profile["source_type"] == "instagram_profile"
    assert profile["label"] == "@thehistologian"
    assert profile["enabled"] is True
    assert tag["source_type"] == "instagram_hashtag"
    assert tag["label"] == "#history"


def test_add_source_is_idempotent_per_url() -> None:
    account_id = _make_account()
    first = sourcing.add_source(account_id, "https://www.instagram.com/thehistologian/")
    second = sourcing.add_source(account_id, "https://www.instagram.com/thehistologian")
    assert first["id"] == second["id"]
    assert len(sourcing.list_sources(account_id)) == 1


def test_add_source_requires_url() -> None:
    account_id = _make_account()
    with pytest.raises(SourcingError):
        sourcing.add_source(account_id, "   ")


def test_enable_disable_and_remove_source() -> None:
    account_id = _make_account()
    src = sourcing.add_source(account_id, "https://www.instagram.com/thehistologian/")

    disabled = sourcing.set_source_enabled(src["id"], False)
    assert disabled["enabled"] is False

    sourcing.remove_source(src["id"])
    assert sourcing.list_sources(account_id) == []


def test_remove_source_detaches_candidates() -> None:
    account_id = _make_account()
    src = sourcing.add_source(account_id, "https://www.instagram.com/thehistologian/")
    with get_session() as session:
        session.add(
            ScrapeCandidate(
                scrape_source_url="https://www.instagram.com/thehistologian/",
                source_url="https://instagram.com/reel/x",
                state="candidate",
                source_id=src["id"],
                account_id=account_id,
            )
        )
        session.commit()

    sourcing.remove_source(src["id"])

    with get_session() as session:
        candidate = session.scalars(select(ScrapeCandidate)).first()
        assert candidate is not None
        assert candidate.source_id is None


def test_list_candidates_filters_by_state_and_normalizes_new() -> None:
    account_id = _make_account()
    with get_session() as session:
        session.add_all(
            [
                ScrapeCandidate(
                    scrape_source_url="s", source_url="u1", state="new", account_id=account_id
                ),
                ScrapeCandidate(
                    scrape_source_url="s", source_url="u2", state="ignored", account_id=account_id
                ),
            ]
        )
        session.commit()

    all_rows = sourcing.list_candidates(account_id)
    assert {r["state"] for r in all_rows} == {"candidate", "ignored"}
    assert [r["state"] for r in sourcing.list_candidates(account_id, "ignored")] == ["ignored"]


def test_list_candidates_filters_before_applying_limit() -> None:
    account_id = _make_account()
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s", source_url="review", state="candidate", account_id=account_id
        )
        session.add(candidate)
        session.flush()
        session.add_all(
            [
                ScrapeCandidate(
                    scrape_source_url="s",
                    source_url=f"pooled-{index}",
                    state="pooled",
                    account_id=account_id,
                )
                for index in range(3)
            ]
        )
        session.commit()

    rows = sourcing.list_candidates(account_id, "candidate", limit=2)

    assert [row["source_url"] for row in rows] == ["review"]


def test_set_candidate_state_ignore_and_restore() -> None:
    account_id = _make_account()
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s", source_url="u", state="candidate", account_id=account_id
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    assert sourcing.set_candidate_state(candidate_id, "ignored")["state"] == "ignored"
    assert sourcing.set_candidate_state(candidate_id, "candidate")["state"] == "candidate"


def test_set_candidate_state_rejects_unsafe_transitions() -> None:
    account_id = _make_account()
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s", source_url="u", state="candidate", account_id=account_id
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    with pytest.raises(SourcingError):
        sourcing.set_candidate_state(candidate_id, "pooled")


def test_accept_candidate_adds_to_account_niche_pool() -> None:
    from nicheflow_studio.db.models import PoolItem

    account_id = _make_account()
    with get_session() as session:
        session.get(Account, account_id).niche = "history"
        candidate = ScrapeCandidate(
            scrape_source_url="https://www.instagram.com/thehistologian/",
            source_url="https://www.instagram.com/reel/ACCEPT1/",
            video_id="ACCEPT1",
            state="candidate",
            account_id=account_id,
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    result = sourcing.accept_candidate(candidate_id)

    assert result["candidate_id"] == candidate_id
    assert result["state"] == "pooled"
    assert result["niche"] == "history"
    with get_session() as session:
        candidate = session.get(ScrapeCandidate, candidate_id)
        pool_item = session.get(PoolItem, result["pool_item_id"])
        assert candidate.state == "pooled"
        assert pool_item.niche == "history"
        assert pool_item.media_asset.source_shortcode == "ACCEPT1"


def test_accept_candidate_ignores_free_text_niche_label() -> None:
    """Regression: a populated free-text ``niche_label`` must not be used as the
    pool niche. Accept keys off the strict ``niche`` instead — otherwise the
    label sentence reaches ``_validate_niche`` and surfaces as a generic
    "Unexpected error" in the UI."""
    account_id = _make_account()
    with get_session() as session:
        account = session.get(Account, account_id)
        account.niche = "history"
        account.niche_label = "History moments, old clips, strange facts, and forgotten stories"
        candidate = ScrapeCandidate(
            scrape_source_url="https://www.instagram.com/thehistologian/",
            source_url="https://www.instagram.com/reel/LABELED1/",
            video_id="LABELED1",
            state="candidate",
            account_id=account_id,
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    result = sourcing.accept_candidate(candidate_id)

    assert result["state"] == "pooled"
    assert result["niche"] == "history"


def test_accept_candidate_requires_account_niche() -> None:
    account_id = _make_account()
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s",
            source_url="https://www.instagram.com/reel/NONICHE/",
            video_id="NONICHE",
            state="candidate",
            account_id=account_id,
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    with pytest.raises(SourcingError, match="no pool niche"):
        sourcing.accept_candidate(candidate_id)


def test_reject_candidate_rejects_unknown_reason() -> None:
    account_id = _make_account()
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s", source_url="u", state="candidate", account_id=account_id
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    with pytest.raises(SourcingError):
        sourcing.reject_candidate(candidate_id, "not_a_reason")


def test_reject_candidate_sets_state_without_pool() -> None:
    account_id = _make_account()
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s", source_url="u-nopool", state="candidate", account_id=account_id
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    result = sourcing.reject_candidate(candidate_id, "low_quality")

    assert result["state"] == "rejected_low_quality"
    assert result["removed_pool_items"] == 0


def test_reject_candidate_removes_pooled_copy() -> None:
    from nicheflow_studio.db.models import PoolItem
    from nicheflow_studio.db.pools import POOL_STATUS_REMOVED, accept_candidate_into_pool

    account_id = _make_account()
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="s",
            source_url="https://www.instagram.com/reel/ABC123/",
            video_id="ABC123",
            state="candidate",
            account_id=account_id,
        )
        session.add(candidate)
        session.flush()
        pool_item = accept_candidate_into_pool(session, candidate=candidate, niche="history")
        session.commit()
        candidate_id = candidate.id
        pool_item_id = pool_item.id

    result = sourcing.reject_candidate(candidate_id, "duplicate")

    assert result["state"] == "rejected_duplicate"
    assert result["removed_pool_items"] == 1
    with get_session() as session:
        item = session.get(PoolItem, pool_item_id)
        assert item is not None
        assert item.acceptance_status == POOL_STATUS_REMOVED
