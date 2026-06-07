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
