from __future__ import annotations

from nicheflow_studio.db.blocklist import block_asset, is_blocked
from nicheflow_studio.db.models import Account, BlockedAsset
from nicheflow_studio.db.pool_intake import ReelMetadata, add_reel_to_pool
from nicheflow_studio.db.session import get_session


def test_block_and_check_by_shortcode() -> None:
    with get_session() as session:
        assert is_blocked(session, source_url="https://instagram.com/reel/ABC/", shortcode="ABC") is False
        block_asset(session, source_url="https://instagram.com/reel/ABC/", shortcode="ABC", reason="ad")
        session.commit()

    with get_session() as session:
        assert is_blocked(session, source_url="https://instagram.com/reel/ABC/", shortcode="ABC") is True
        # Same shortcode under a different URL form still matches.
        assert is_blocked(session, source_url="https://www.instagram.com/p/ABC/") is True


def test_block_by_url_when_no_shortcode() -> None:
    with get_session() as session:
        block_asset(session, source_url="https://example.com/video123", shortcode=None)
        session.commit()

    with get_session() as session:
        assert is_blocked(session, source_url="https://example.com/video123") is True
        assert is_blocked(session, source_url="https://example.com/other") is False


def test_block_is_idempotent() -> None:
    with get_session() as session:
        block_asset(session, source_url="https://instagram.com/reel/XYZ/", shortcode="XYZ")
        block_asset(session, source_url="https://instagram.com/reel/XYZ/", shortcode="XYZ")
        session.commit()

    with get_session() as session:
        assert session.query(BlockedAsset).count() == 1


def test_add_reel_to_pool_skips_blocked_footage() -> None:
    with get_session() as session:
        session.add(Account(name="Hist", platform="instagram", niche="history"))
        block_asset(session, source_url="https://instagram.com/reel/BLK/", shortcode="BLK", reason="ad")
        session.commit()

    with get_session() as session:
        result = add_reel_to_pool(
            session,
            niche="history",
            metadata=ReelMetadata(source_url="https://instagram.com/reel/BLK/", shortcode="BLK"),
        )
        session.commit()

    assert result.status == "blocked"
