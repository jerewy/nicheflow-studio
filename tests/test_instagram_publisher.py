from pathlib import Path

from nicheflow_studio.publisher.instagram_publisher import PublishResult, publish_reel


def test_publish_result_ok_is_true_for_posted_and_dry_run() -> None:
    assert PublishResult("posted").ok is True
    assert PublishResult("dry_run").ok is True
    assert PublishResult("failed", error_message="x").ok is False
    assert PublishResult("checkpoint", error_message="x").ok is False


def test_publish_result_flags_checkpoint() -> None:
    assert PublishResult("checkpoint", error_message="suspicious").is_checkpoint is True
    assert PublishResult("posted").is_checkpoint is False


def test_publish_reel_fails_fast_when_video_missing(tmp_path: Path) -> None:
    # Missing file must short-circuit BEFORE any browser launch, so this stays
    # a pure unit test (no Playwright, no network).
    missing = tmp_path / "nope.mp4"

    result = publish_reel("main", missing, "caption")

    assert result.status == "failed"
    assert result.ok is False
    assert "video not found" in (result.error_message or "")
