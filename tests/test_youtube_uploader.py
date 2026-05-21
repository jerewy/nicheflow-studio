from pathlib import Path

from nicheflow_studio.uploader.youtube import YouTubeUploadPayload, upload_youtube_video


class FakeInsertRequest:
    def __init__(self) -> None:
        self.calls = 0

    def next_chunk(self):  # noqa: ANN201
        self.calls += 1
        return None, {"id": "youtube123"}


class FakeVideos:
    def __init__(self) -> None:
        self.insert_request = FakeInsertRequest()
        self.insert_kwargs = None

    def insert(self, **kwargs):  # noqa: ANN003, ANN201
        self.insert_kwargs = kwargs
        return self.insert_request


class FakeYouTubeService:
    def __init__(self) -> None:
        self.videos_resource = FakeVideos()

    def videos(self) -> FakeVideos:
        return self.videos_resource


def test_upload_youtube_video_sends_metadata_and_returns_video_id(tmp_path: Path) -> None:
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    service = FakeYouTubeService()

    video_id = upload_youtube_video(
        YouTubeUploadPayload(
            file_path=video_path,
            title="Upload title",
            description="Upload description",
            privacy_status="unlisted",
            made_for_kids=False,
        ),
        youtube_service=service,
        media_upload_factory=lambda path, **_: path,
    )

    assert video_id == "youtube123"
    insert_kwargs = service.videos_resource.insert_kwargs
    assert insert_kwargs["part"] == ",".join(["snippet", "status"])
    assert insert_kwargs["body"]["snippet"]["title"] == "Upload title"
    assert insert_kwargs["body"]["snippet"]["description"] == "Upload description"
    assert insert_kwargs["body"]["status"]["privacyStatus"] == "unlisted"
    assert insert_kwargs["body"]["status"]["selfDeclaredMadeForKids"] is False
    assert insert_kwargs["media_body"] == str(video_path)
