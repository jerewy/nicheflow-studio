from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import clip_intake
from nicheflow_studio.services.clip_intake import ClipIntakeError


def _make_account(name: str = "Clips") -> int:
    with get_session() as session:
        account = Account(name=name, platform="instagram")
        session.add(account)
        session.commit()
        return account.id


def _fake_video(tmp_path: Path, name: str = "clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not really a video, but a real file on disk")
    return path


def _stub_h264(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every input is already browser-safe H.264."""
    monkeypatch.setattr(clip_intake, "_video_codec", lambda _path: "h264")


def _only_item() -> DownloadItem:
    with get_session() as session:
        items = session.scalars(select(DownloadItem)).all()
        assert len(items) == 1
        return items[0]


def test_register_clip_creates_a_library_item(monkeypatch, tmp_path: Path) -> None:
    _stub_h264(monkeypatch)
    account_id = _make_account()

    result = clip_intake.register_clip(
        _fake_video(tmp_path),
        account_id=account_id,
        title="Crowd went silent",
        source_url="https://youtu.be/abc123",
        transcript_context="he walked off stage and nobody moved",
    )

    item = _only_item()
    assert item.id == result["item_id"]
    assert item.title == "Crowd went silent"
    assert item.account_id == account_id
    assert item.source_url == "https://youtu.be/abc123"
    # status/review_state are what the Library and Processing screens filter on;
    # a wrong value here imports the clip into a screen nobody looks at.
    assert item.status == "downloaded"
    assert item.review_state == "new"


def test_register_clip_copies_the_file_into_the_library(monkeypatch, tmp_path: Path) -> None:
    _stub_h264(monkeypatch)
    source = _fake_video(tmp_path)

    result = clip_intake.register_clip(source, account_id=_make_account())

    stored = Path(result["file_path"])
    assert stored.exists()
    assert stored != source
    # The original must survive: the operator's Downloads folder is theirs.
    assert source.exists()
    assert stored.read_bytes() == source.read_bytes()


def test_register_clip_grounds_the_draft_prompt_with_spoken_words(
    monkeypatch, tmp_path: Path
) -> None:
    _stub_h264(monkeypatch)
    spoken = "and that is when the whole arena went quiet"

    clip_intake.register_clip(
        _fake_video(tmp_path), account_id=_make_account(), transcript_context=spoken
    )

    assert _only_item().transcript_text == spoken


def test_register_clip_marks_a_manual_import_as_local(monkeypatch, tmp_path: Path) -> None:
    _stub_h264(monkeypatch)

    result = clip_intake.register_clip(_fake_video(tmp_path), account_id=_make_account())

    assert _only_item().extractor == "local"
    # No real URL, so the row still needs a stable reference of its own.
    assert result["source_url"].startswith("local://")


def test_register_clip_converts_a_mov_instead_of_copying(monkeypatch, tmp_path: Path) -> None:
    """A campaign clip pack ships .mov; it must not land as an unplayable file."""
    monkeypatch.setattr(clip_intake, "_video_codec", lambda _path: "hevc")
    converted: dict = {}

    def fake_normalize(source: Path, destination: Path) -> None:
        converted["source"] = source
        converted["destination"] = destination
        destination.write_bytes(b"converted")

    monkeypatch.setattr(clip_intake, "_normalize_to_mp4", fake_normalize)

    result = clip_intake.register_clip(
        _fake_video(tmp_path, "macklemore_clip_08.mov"), account_id=_make_account()
    )

    assert converted["source"].suffix == ".mov"
    # Whatever went in, a .mp4 comes out, because the review screen has to play it.
    assert Path(result["file_path"]).suffix == ".mp4"
    assert converted["destination"] == Path(result["file_path"])


def test_normalize_stream_copies_when_already_h264(monkeypatch, tmp_path: Path) -> None:
    """Re-encoding an H.264 source would cost minutes and lose quality for nothing."""
    monkeypatch.setattr(clip_intake, "ffmpeg_binary", lambda: Path("ffmpeg"))
    monkeypatch.setattr(clip_intake, "_video_codec", lambda _path: "h264")
    captured: dict = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(clip_intake.subprocess, "run", fake_run)

    clip_intake._normalize_to_mp4(tmp_path / "in.mov", tmp_path / "out.mp4")

    command = captured["command"]
    assert "copy" in command
    assert "libx264" not in command


def test_normalize_re_encodes_a_codec_the_preview_cannot_play(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(clip_intake, "ffmpeg_binary", lambda: Path("ffmpeg"))
    monkeypatch.setattr(clip_intake, "_video_codec", lambda _path: "hevc")
    captured: dict = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(clip_intake.subprocess, "run", fake_run)

    clip_intake._normalize_to_mp4(tmp_path / "in.mov", tmp_path / "out.mp4")

    assert "libx264" in captured["command"]


def test_register_clip_rejects_an_unsupported_file(monkeypatch, tmp_path: Path) -> None:
    _stub_h264(monkeypatch)
    document = tmp_path / "notes.txt"
    document.write_text("not a video")

    with pytest.raises(ClipIntakeError, match="supported video"):
        clip_intake.register_clip(document, account_id=_make_account())


def test_register_clip_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ClipIntakeError, match="No such video file"):
        clip_intake.register_clip(tmp_path / "gone.mp4", account_id=_make_account())


def test_register_clip_rejects_an_unknown_account(monkeypatch, tmp_path: Path) -> None:
    """An orphan item would import successfully and then be invisible everywhere."""
    _stub_h264(monkeypatch)

    with pytest.raises(ClipIntakeError, match="No account with id"):
        clip_intake.register_clip(_fake_video(tmp_path), account_id=4242)

    with get_session() as session:
        assert session.scalars(select(DownloadItem)).all() == []


def test_register_moment_cuts_the_span_then_registers_it(monkeypatch, tmp_path: Path) -> None:
    _stub_h264(monkeypatch)
    account_id = _make_account()
    source = _fake_video(tmp_path, "source.mp4")
    cuts: dict = {}

    def fake_cut(src: Path, out: Path, start: float, end: float, **_kwargs) -> Path:
        cuts["args"] = (src, start, end)
        out.write_bytes(b"the cut segment")
        return out

    monkeypatch.setattr(
        "nicheflow_studio.services.clip_studio.cut_moment", fake_cut
    )

    result = clip_intake.register_moment(
        source,
        start_seconds=61.5,
        end_seconds=79.0,
        account_id=account_id,
        title="The comeback",
        transcript_context="nobody saw it coming",
    )

    assert cuts["args"][1:] == (61.5, 79.0)
    item = _only_item()
    assert item.id == result["item_id"]
    # Sourced clips are distinguishable from hand-imported ones in the library.
    assert item.extractor == "clip_studio"
    assert item.transcript_text == "nobody saw it coming"
    assert Path(item.file_path).read_bytes() == b"the cut segment"


def _send_moment(monkeypatch, tmp_path: Path, source: Path, **kwargs) -> dict:
    """Cut and register one moment with ffmpeg stubbed out."""
    _stub_h264(monkeypatch)

    def fake_cut(src: Path, out: Path, start: float, end: float, **_kwargs) -> Path:
        out.write_bytes(b"cut")
        return out

    monkeypatch.setattr("nicheflow_studio.services.clip_studio.cut_moment", fake_cut)
    return clip_intake.register_moment(source, **kwargs)


def test_register_moment_refuses_the_same_window_twice(monkeypatch, tmp_path: Path) -> None:
    """Eight candidates come off one source and the grid does not survive a
    reload, so re-sending one already sent is easy — and it ends as the same
    clip published twice from one account."""
    account_id = _make_account()
    source = _fake_video(tmp_path, "source.mp4")
    window = {
        "start_seconds": 61.5,
        "end_seconds": 79.0,
        "account_id": account_id,
        "clip_source_ref": "https://youtu.be/abc",
    }

    first = _send_moment(monkeypatch, tmp_path, source, **window)

    with pytest.raises(ClipIntakeError, match=f"already sent to this account as item #{first['item_id']}"):
        _send_moment(monkeypatch, tmp_path, source, **window)

    with get_session() as session:
        assert len(session.scalars(select(DownloadItem)).all()) == 1


def test_register_moment_allows_a_nudged_trim(monkeypatch, tmp_path: Path) -> None:
    """A moved in-point is a different cut, not a re-send."""
    account_id = _make_account()
    source = _fake_video(tmp_path, "source.mp4")
    base = {"account_id": account_id, "clip_source_ref": "https://youtu.be/abc"}

    _send_moment(monkeypatch, tmp_path, source, start_seconds=61.5, end_seconds=79.0, **base)
    _send_moment(monkeypatch, tmp_path, source, start_seconds=64.0, end_seconds=79.0, **base)

    with get_session() as session:
        assert len(session.scalars(select(DownloadItem)).all()) == 2


def test_register_moment_allows_the_same_window_on_another_account(
    monkeypatch, tmp_path: Path
) -> None:
    """Two accounts is a deliberate call; the guard is per account."""
    source = _fake_video(tmp_path, "source.mp4")
    window = {
        "start_seconds": 61.5,
        "end_seconds": 79.0,
        "clip_source_ref": "https://youtu.be/abc",
    }

    _send_moment(monkeypatch, tmp_path, source, account_id=_make_account(), **window)
    _send_moment(monkeypatch, tmp_path, source, account_id=_make_account(), **window)

    with get_session() as session:
        assert len(session.scalars(select(DownloadItem)).all()) == 2


def test_register_moment_reopens_a_window_whose_clip_was_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    """Rejecting a clip and cutting it again is a correction, not a duplicate."""
    account_id = _make_account()
    source = _fake_video(tmp_path, "source.mp4")
    window = {
        "start_seconds": 61.5,
        "end_seconds": 79.0,
        "account_id": account_id,
        "clip_source_ref": "https://youtu.be/abc",
    }

    first = _send_moment(monkeypatch, tmp_path, source, **window)
    with get_session() as session:
        item = session.get(DownloadItem, first["item_id"])
        item.review_state = "rejected"
        session.commit()

    _send_moment(monkeypatch, tmp_path, source, **window)

    with get_session() as session:
        assert len(session.scalars(select(DownloadItem)).all()) == 2


def test_register_moment_force_overrides_the_duplicate_guard(
    monkeypatch, tmp_path: Path
) -> None:
    account_id = _make_account()
    source = _fake_video(tmp_path, "source.mp4")
    window = {
        "start_seconds": 61.5,
        "end_seconds": 79.0,
        "account_id": account_id,
        "clip_source_ref": "https://youtu.be/abc",
    }

    _send_moment(monkeypatch, tmp_path, source, **window)
    _send_moment(monkeypatch, tmp_path, source, force=True, **window)

    with get_session() as session:
        assert len(session.scalars(select(DownloadItem)).all()) == 2


def test_register_moment_records_the_window_it_cut(monkeypatch, tmp_path: Path) -> None:
    """The guard reads these back; the filename cannot supply them because the
    library renames the staged cut on the way in."""
    result = _send_moment(
        monkeypatch,
        tmp_path,
        _fake_video(tmp_path, "source.mp4"),
        start_seconds=61.5,
        end_seconds=79.0,
        account_id=_make_account(),
    )

    with get_session() as session:
        item = session.get(DownloadItem, result["item_id"])
        assert (item.clip_start_seconds, item.clip_end_seconds) == (61.5, 79.0)


def test_register_moment_rejects_an_inverted_range(tmp_path: Path) -> None:
    with pytest.raises(ClipIntakeError, match="must be after start"):
        clip_intake.register_moment(
            _fake_video(tmp_path),
            start_seconds=90.0,
            end_seconds=12.0,
            account_id=_make_account(),
        )


def test_register_moment_leaves_no_staging_file_behind(monkeypatch, tmp_path: Path) -> None:
    """The staging cut sits in the same folder as the import; a leftover reads
    as a second, identical clip in the library folder."""
    _stub_h264(monkeypatch)

    def fake_cut(src: Path, out: Path, start: float, end: float, **_kwargs) -> Path:
        out.write_bytes(b"cut")
        return out

    monkeypatch.setattr("nicheflow_studio.services.clip_studio.cut_moment", fake_cut)

    result = clip_intake.register_moment(
        _fake_video(tmp_path, "source.mp4"),
        start_seconds=0.0,
        end_seconds=11.0,
        account_id=_make_account(),
    )

    library_folder = Path(result["file_path"]).parent
    assert [path.name for path in library_folder.iterdir()] == [
        Path(result["file_path"]).name
    ]


def test_register_moment_passes_caption_burn_through(monkeypatch, tmp_path) -> None:
    """Processing never sees the source transcript, so captions burn in here.

    Without the pass-through the UI toggle would be cosmetic: the clip would
    reach export with no way left to caption it.
    """
    from nicheflow_studio.services import clip_studio

    seen: dict = {}

    def fake_cut(source, out, start, end, **kwargs):
        seen.update(kwargs)
        out.write_bytes(b"cut")
        return out

    monkeypatch.setattr(clip_studio, "cut_moment", fake_cut)
    monkeypatch.setattr(
        clip_intake, "register_clip", lambda staged, **kwargs: {"item_id": 1, "kwargs": kwargs}
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    transcript = tmp_path / "t.srt"
    transcript.write_text("1\n00:00:00,000 --> 00:00:02,000\nhi\n\n", encoding="utf-8")

    clip_intake.register_moment(
        source, start_seconds=0.0, end_seconds=10.0, account_id=1,
        transcript_path=transcript, burn_captions=True,
    )
    assert seen["burn_captions"] is True
    assert seen["transcript_path"] == transcript
