from pathlib import Path

from PIL import Image

from nicheflow_studio.processing import title_persona, video


def test_persona_examples_fit_the_render_ceiling_and_carry_no_dashes() -> None:
    # The model copies examples more reliably than it follows rules, so an
    # example that breaks a rule teaches the break.
    titles = [title for group in title_persona.PERSONA_EXAMPLES.values() for title in group]

    assert len(titles) >= 9
    for title in titles:
        assert len(title) <= title_persona.TITLE_CHAR_CEILING, title
        assert "\u2014" not in title and "\u2013" not in title and "--" not in title, title


def test_add_label_strip_prints_a_band_above_the_sheet(tmp_path: Path) -> None:
    sheet = tmp_path / "reel_4_item2202.jpg"
    Image.new("RGB", (300, 200), "white").save(sheet)

    video.add_label_strip(sheet, "REEL 4 (#2202)")

    with Image.open(sheet) as labelled:
        assert labelled.size == (300, 200 + video._SHEET_LABEL_HEIGHT)
        # The band is black apart from the white label text, and the sheet
        # itself starts below it untouched.
        assert labelled.getpixel((295, 2)) == (0, 0, 0)
        assert min(labelled.getpixel((150, 150))) > 240


def test_persona_keeps_the_rules_the_blind_test_showed_it_needs() -> None:
    # Each rule is here because a blind-test reel failed without it, and the link
    # rule had already been lost once, between v3 and the first persona draft.
    rules = "\n".join(title_persona.persona_title_rules())

    assert "A link between two facts is a claim too" in rules
    assert "use their full name" in rules
    assert "leaves out the detail from your Clip line" in rules
