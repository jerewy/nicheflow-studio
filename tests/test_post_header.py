from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from nicheflow_studio.processing import post_header, video
from nicheflow_studio.processing.post_header import PostHeader
from nicheflow_studio.processing.video import CropSettings, VideoProbe


pytest.importorskip("PIL")

FONT_PATH = Path("C:/Windows/Fonts/arialbd.ttf")
pytestmark = pytest.mark.skipif(
    not FONT_PATH.is_file(), reason="Header rendering needs a real TrueType font."
)

HEADER = PostHeader(display_name="History Trails", avatar_path=None, verified=True)

# Every render below anchors the title margin at 74 (the HistoryTrails inset).
TITLE_LEFT = 74


def _avatar_box() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the avatar for a TITLE_LEFT-anchored render."""
    left = TITLE_LEFT + post_header.INDENT_PX
    return left, post_header.TOP_PAD_PX, left + post_header.AVATAR_DIAMETER_PX, (
        post_header.TOP_PAD_PX + post_header.AVATAR_DIAMETER_PX
    )


def _avatar_centre() -> tuple[int, int]:
    left, top, right, bottom = _avatar_box()
    return (left + right) // 2, (top + bottom) // 2


def _solid_avatar(path: Path, size: int = 150) -> Path:
    """A 150x150 source, matching what Instagram serves for a profile picture."""
    from PIL import Image

    Image.new("RGB", (size, size), (200, 40, 40)).save(path, "PNG")
    return path


def test_render_post_header_image_writes_band_sized_png(tmp_path: Path) -> None:
    from PIL import Image

    output = tmp_path / "post-header.png"

    post_header.render_post_header_image(
        HEADER,
        canvas_width=1080,
        canvas_height=400,
        left_x=TITLE_LEFT,
        font_path=FONT_PATH,
        output_path=output,
    )

    with Image.open(output) as image:
        assert image.size == (1080, 400)
        assert image.mode == "RGBA"
        # Avatar centre is opaque, the band below the header strip is not, so
        # the PNG can be overlaid on the black band without masking the title.
        assert image.getpixel(_avatar_centre())[3] == 255
        assert image.getpixel((540, 380))[3] == 0


def test_identity_row_is_indented_past_the_title_margin(tmp_path: Path) -> None:
    from PIL import Image

    output = tmp_path / "post-header.png"

    post_header.render_post_header_image(
        HEADER,
        canvas_width=1080,
        canvas_height=400,
        left_x=TITLE_LEFT,
        font_path=FONT_PATH,
        output_path=output,
    )

    row_y = post_header.TOP_PAD_PX + post_header.AVATAR_DIAMETER_PX // 2
    with Image.open(output) as image:
        opaque = [x for x in range(1080) if image.getpixel((x, row_y))[3] > 0]

    assert opaque, "nothing drawn on the avatar row"
    # Nothing is painted between the title margin and the indented avatar.
    assert opaque[0] >= TITLE_LEFT + post_header.INDENT_PX
    assert opaque[0] < TITLE_LEFT + post_header.INDENT_PX + 8


def test_render_post_header_image_uses_the_avatar_file(tmp_path: Path) -> None:
    from PIL import Image

    avatar = _solid_avatar(tmp_path / "pastmomentsdaily.png")
    output = tmp_path / "post-header.png"

    post_header.render_post_header_image(
        PostHeader(display_name="Past Moments Daily", avatar_path=avatar, verified=True),
        canvas_width=1080,
        canvas_height=400,
        left_x=TITLE_LEFT,
        font_path=FONT_PATH,
        output_path=output,
    )

    with Image.open(output) as image:
        red, green, blue, alpha = image.getpixel(_avatar_centre())
    assert alpha == 255
    assert red > 150 and green < 90 and blue < 90


def test_avatar_is_masked_to_a_circle(tmp_path: Path) -> None:
    from PIL import Image

    avatar = _solid_avatar(tmp_path / "handle.png")
    output = tmp_path / "post-header.png"

    post_header.render_post_header_image(
        PostHeader(display_name="Handle", avatar_path=avatar, verified=False),
        canvas_width=1080,
        canvas_height=400,
        left_x=TITLE_LEFT,
        font_path=FONT_PATH,
        output_path=output,
    )

    left, top, _right, _bottom = _avatar_box()
    with Image.open(output) as image:
        # The square source's top-left corner falls outside the circle.
        assert image.getpixel((left + 2, top + 2))[3] == 0


def test_missing_avatar_file_falls_back_instead_of_raising(tmp_path: Path) -> None:
    output = tmp_path / "post-header.png"

    post_header.render_post_header_image(
        PostHeader(display_name="Ghost", avatar_path=tmp_path / "nope.png", verified=True),
        canvas_width=1080,
        canvas_height=400,
        left_x=TITLE_LEFT,
        font_path=FONT_PATH,
        output_path=output,
    )

    assert output.is_file()


def _band_height(filter_chain: str) -> int:
    match = re.search(r"color=c=black:s=1080x(\d+)", filter_chain)
    assert match is not None, filter_chain
    return int(match.group(1))


def _title_band_chain(tmp_path: Path, *, header: PostHeader | None) -> str:
    return video._title_band_filter_complex(
        "They found the letters sealed in the wall a century later",
        crop="crop=1080:1920:0:0",
        crop_width=1080,
        crop_height=1920,
        font_path=Path("C:/Windows/Fonts/arial.ttf"),
        requested_font_size=54,
        title_font_name="arial",
        title_color="#FFFFFF",
        title_text_dir=tmp_path,
        duration_seconds=15.0,
        title_align="left",
        post_header=header,
    )


def test_title_band_grows_to_fit_the_header_strip(tmp_path: Path) -> None:
    plain = _band_height(_title_band_chain(tmp_path, header=None))
    with_header = _band_height(_title_band_chain(tmp_path, header=HEADER))

    assert with_header > plain
    # The title anchor clears the avatar row, so the two never overlap.
    assert post_header.header_block_height() > (
        post_header.TOP_PAD_PX + post_header.AVATAR_DIAMETER_PX
    )


def test_title_text_is_anchored_directly_below_the_header(tmp_path: Path) -> None:
    chain = _title_band_chain(tmp_path, header=HEADER)

    # The first title line starts at the header strip's bottom edge, so the
    # header-to-title gap does not drift with the title's line count.
    assert f"y={post_header.header_block_height()}[" in chain


def test_title_band_composites_the_header_over_the_title(tmp_path: Path) -> None:
    chain = _title_band_chain(tmp_path, header=HEADER)

    assert "post-header.png" in chain
    assert "[headerimg]" in chain
    assert "[titlebody][headerimg]overlay=0:0:format=auto,format=yuv420p[title]" in chain
    # The header overlay must land before the band is stacked onto the footage.
    assert chain.index("[headerimg]") < chain.index("[title][content]vstack=inputs=2[block]")


def test_title_band_without_header_is_unchanged(tmp_path: Path) -> None:
    chain = _title_band_chain(tmp_path, header=None)

    assert "post-header.png" not in chain
    assert "titlebody" not in chain
    assert "[title][content]vstack=inputs=2[block]" in chain


def test_export_cropped_video_passes_the_header_into_the_graph(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(
        video, "windows_font_file", lambda _font_name=None: Path("C:/Windows/Fonts/arial.ttf")
    )
    monkeypatch.setattr(
        video, "probe_video", lambda _: VideoProbe(width=1080, height=1920, duration_seconds=15.0)
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["graph"] = command[command.index("-filter_complex") + 1]
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    video.export_cropped_video(
        input_path=input_path,
        output_path=tmp_path / "processed" / "sample_cropped.mp4",
        crop=CropSettings(),
        title_text="They found the letters sealed in the wall a century later",
        title_font_size=54,
        title_font_name="arial",
        title_layout="top_band",
        title_align="left",
        post_header=HEADER,
    )

    graph = captured["graph"]
    assert "[headerimg]" in graph
    assert "[vout]" in graph


def test_single_line_title_stays_left_aligned_under_the_header(tmp_path: Path) -> None:
    """HistoryTrails centres one-line titles; a header must suppress that.

    The header is a left-aligned block, so a centred title beneath it reads as
    two columns instead of one.
    """
    short = "They found the letters"

    def chain(header: PostHeader | None) -> str:
        return video._title_band_filter_complex(
            short,
            crop="crop=1080:1920:0:0",
            crop_width=1080,
            crop_height=1920,
            font_path=Path("C:/Windows/Fonts/arial.ttf"),
            requested_font_size=54,
            title_font_name="arial",
            title_color="#FFFFFF",
            title_text_dir=tmp_path,
            duration_seconds=15.0,
            title_align="left",
            post_header=header,
        )

    # Unchanged without a header: still centred.
    assert "x=(w-text_w)/2" in chain(None)
    # With a header: pinned to the same left margin the header aligns against.
    assert "x=(w-text_w)/2" not in chain(HEADER)
    assert "x=74" in chain(HEADER)


@pytest.mark.parametrize(
    ("width", "height"),
    [(1920, 1080), (1080, 1080), (1080, 1350), (1080, 1920), (1080, 2400), (720, 2560)],
)
def test_header_block_fits_the_canvas_for_every_aspect_ratio(
    tmp_path: Path, width: int, height: int
) -> None:
    """A tall source plus a long title must never overflow the 1920 canvas.

    The header steals vertical room from the footage, so the footage has to
    shrink rather than the stacked block running off the bottom.
    """
    long_title = "Neverland " * 30

    chain = video._title_band_filter_complex(
        long_title.strip(),
        crop=f"crop={width}:{height}:0:0",
        crop_width=width,
        crop_height=height,
        font_path=Path("C:/Windows/Fonts/arial.ttf"),
        requested_font_size=54,
        title_font_name="arial",
        title_color="#FFFFFF",
        title_text_dir=tmp_path,
        duration_seconds=15.0,
        title_align="left",
        title_line_gap_scale=0.20,
        post_header=HEADER,
    )

    band = _band_height(chain)
    block_y = int(re.search(r"\[block\]pad=1080:1920:\(ow-iw\)/2:(\d+)", chain).group(1))
    content_height = int(
        re.search(r"pad=1080:(\d+):\d+:0:color=black\[content\]", chain).group(1)
    )

    assert content_height > 0
    assert block_y + band + content_height <= 1920
    # The header strip itself always survives, whatever the footage does.
    assert band > post_header.header_block_height()
