"""Cover the line trimming that keeps a source's brand mark out of the hook.

The OCR band crop reaches into the upper third of the frame, so an account's
watermark lands in the same reading as the hook. Everything downstream treats
that string as the source's own claim, so a leaked brand mark is not cosmetic.
"""

from pathlib import Path

from PIL import Image

from nicheflow_studio.processing import on_screen_hook


def test_leading_brand_mark_is_dropped() -> None:
    lines = ["InsideHistory", "The chilling stare of Ted Bundy", "as his victim testifies"]
    assert on_screen_hook._drop_source_watermark(lines) == lines[1:]


def test_trailing_handle_is_dropped() -> None:
    lines = ["They never told you the Queen's biggest fan", "@vaultedhistory"]
    assert on_screen_hook._drop_source_watermark(lines) == lines[:1]


def test_a_single_token_inside_the_hook_survives() -> None:
    lines = ["This is why", "mercury", "is not allowed on airplanes"]
    assert on_screen_hook._drop_source_watermark(lines) == lines


def test_a_long_spaceless_line_is_not_a_brand_mark() -> None:
    # OCR squashes spaces out of a real line often enough that length is the
    # only thing separating "InsideHistory" from a mangled sentence.
    lines = ["Therealreasona20-year-old claimed the crown", "backin1968"]
    assert on_screen_hook._drop_source_watermark(lines) == lines


def test_a_last_word_that_finishes_the_sentence_survives() -> None:
    # Item 2474: the banner wrapped "death" onto its own line, the trim took it
    # for a brand mark, and the batch titled the reel "This was Michael Jackson
    # thirty six hours before." A line ending mid-phrase cannot be the end.
    lines = ["This was 36 hours before Michael Jackson's", "death"]
    assert on_screen_hook._drop_source_watermark(lines) == lines
    lines = ["The last song he ever recorded was for the", "fans"]
    assert on_screen_hook._drop_source_watermark(lines) == lines


def test_a_trailing_brand_mark_after_a_finished_line_is_dropped() -> None:
    lines = ["The chilling stare of Ted Bundy in court", "InsideHistory"]
    assert on_screen_hook._drop_source_watermark(lines) == lines[:1]


def test_a_one_line_hook_is_never_emptied() -> None:
    assert on_screen_hook._drop_source_watermark(["@somehandle"]) == ["@somehandle"]


def test_read_hook_applies_the_trim(monkeypatch) -> None:
    monkeypatch.setattr(
        on_screen_hook, "frame_image", lambda path: Image.new("RGB", (1080, 1920))
    )
    monkeypatch.setattr(on_screen_hook, "_bright_text_runs", lambda image: [])
    monkeypatch.setattr(
        on_screen_hook,
        "_run_local_ocr",
        lambda crop: [
            ([[440, 500], [770, 500], [770, 570], [440, 570]], "InsideHistory", 0.99),
            ([[95, 660], [1330, 660], [1330, 740], [95, 740]], "The chilling stare", 0.95),
        ],
    )

    hook, score = on_screen_hook.read_hook(Path("clip.mp4"))

    assert hook == "The chilling stare"
    assert score > 0.9


def test_a_stray_glyph_is_not_a_hook(monkeypatch) -> None:
    monkeypatch.setattr(
        on_screen_hook, "frame_image", lambda path: Image.new("RGB", (1080, 1920))
    )
    monkeypatch.setattr(on_screen_hook, "_bright_text_runs", lambda image: [])
    monkeypatch.setattr(
        on_screen_hook,
        "_run_local_ocr",
        # A single crisp letter off the footage reads with high confidence, so the
        # score floor alone does not catch it.
        lambda crop: [([[500, 500], [530, 500], [530, 560], [500, 560]], "T", 0.86)],
    )

    hook, score = on_screen_hook.read_hook(Path("clip.mp4"))

    assert hook == ""
    assert score > 0.8
