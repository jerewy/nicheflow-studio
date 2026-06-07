from pathlib import Path

from nicheflow_studio.app.local_media import MEDIA_ORIGIN, media_url


def test_media_url_maps_file_inside_data_dir(monkeypatch, tmp_path: Path) -> None:
    data = tmp_path / "data"
    video = data / "downloads" / "clip name.mp4"
    video.parent.mkdir(parents=True)
    video.touch()
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(data))

    assert media_url(str(video)) == f"{MEDIA_ORIGIN}/downloads/clip%20name.mp4"


def test_media_url_rejects_file_outside_data_dir(monkeypatch, tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.touch()
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(data))

    assert media_url(str(outside)) is None
