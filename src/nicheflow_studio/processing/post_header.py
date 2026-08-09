"""Instagram post-header strip: avatar + display name + verified badge.

Rendered as a transparent PNG sized to the black title band and composited on
top of it by ``processing.video._title_band_filter_complex``, so an exported
Reel reads like a screenshot of the account's own feed post (profile picture,
account name, gold verified badge, then the title copy underneath).

Everything is drawn with Pillow — the verified badge is generated from
primitives rather than shipped as an image asset, so no binary lives in the
repo and the badge scales with the layout constants below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

# --- Layout constants (1080-wide canvas) ----------------------------------- #
# The header occupies a fixed-height strip at the TOP of the title band, and
# the title text is anchored directly below it at ``header_block_height()`` —
# the same relationship a caption has to the username row in a real feed post.
AVATAR_DIAMETER_PX = 96
TOP_PAD_PX = 44
BOTTOM_GAP_PX = 46
NAME_FONT_SIZE_PX = 40
# The badge reads as an accent next to the name, not a peer of it: roughly
# three quarters of the name's size, matching how Instagram renders it.
BADGE_SIZE_PX = 30
AVATAR_NAME_GAP_PX = 24
NAME_BADGE_GAP_PX = 14
# The identity row is indented past the title text, which stays flush with the
# footage's left edge. Measured off a real post at ~90px on the 1080 canvas.
INDENT_PX = 90

_VERIFIED_BLUE = (56, 151, 240, 255)
_NAME_COLOR = (255, 255, 255, 255)
_AVATAR_FALLBACK_BG = (58, 58, 58, 255)
# Circles and the badge are drawn at 4x and downscaled with LANCZOS; Pillow has
# no antialiased ellipse, and a hard-edged 96px circle looks obviously fake.
_SUPERSAMPLE = 4


@dataclass(frozen=True)
class PostHeader:
    """Identity strip burned above the title: who the post appears to be from."""

    display_name: str
    avatar_path: Path | None = None
    verified: bool = True


def header_block_height() -> int:
    """Extra title-band height the header strip needs, in canvas pixels."""
    return TOP_PAD_PX + AVATAR_DIAMETER_PX + BOTTOM_GAP_PX


def render_post_header_image(
    header: PostHeader,
    *,
    canvas_width: int,
    canvas_height: int,
    left_x: int,
    font_path: Path,
    output_path: Path,
) -> None:
    """Draw the header onto a transparent PNG the size of the whole title band.

    ``canvas_height`` is the full band height (not just the header strip) so the
    PNG can be composited at 0:0 with a single ``overlay`` node, matching how the
    PIL title image is already layered onto the band.

    ``left_x`` is the title's left margin; the identity row is drawn ``INDENT_PX``
    to the right of it, so callers pass one alignment reference for both.
    """
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    name = header.display_name.strip()
    avatar_x = left_x + INDENT_PX

    avatar = _avatar_image(
        header.avatar_path,
        diameter=AVATAR_DIAMETER_PX,
        fallback_letter=name[:1].upper(),
        font_path=font_path,
    )
    image.alpha_composite(avatar, (avatar_x, TOP_PAD_PX))

    name_font = ImageFont.truetype(str(font_path), NAME_FONT_SIZE_PX)
    name_x = avatar_x + AVATAR_DIAMETER_PX + AVATAR_NAME_GAP_PX
    badge_width = (NAME_BADGE_GAP_PX + BADGE_SIZE_PX) if header.verified else 0
    available_width = canvas_width - name_x - badge_width - left_x
    name = _truncate_to_width(name, name_font, max_width=available_width)

    # Optically center the name against the avatar using the font's own metrics
    # rather than the glyph bbox, so names with and without descenders sit on
    # the same line.
    ascent, descent = name_font.getmetrics()
    name_y = TOP_PAD_PX + (AVATAR_DIAMETER_PX - (ascent + descent)) // 2
    ImageDraw.Draw(image).text((name_x, name_y), name, font=name_font, fill=_NAME_COLOR)

    if header.verified:
        badge = _verified_badge_image(BADGE_SIZE_PX)
        badge_x = name_x + int(round(name_font.getlength(name))) + NAME_BADGE_GAP_PX
        badge_y = TOP_PAD_PX + (AVATAR_DIAMETER_PX - BADGE_SIZE_PX) // 2
        image.alpha_composite(badge, (badge_x, badge_y))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG")


def _truncate_to_width(text: str, font, max_width: int) -> str:  # noqa: ANN001 - PIL font
    """Ellipsize ``text`` so the name never collides with the badge or the edge."""
    if max_width <= 0 or font.getlength(text) <= max_width:
        return text
    trimmed = text
    while trimmed and font.getlength(f"{trimmed}…") > max_width:
        trimmed = trimmed[:-1]
    return f"{trimmed}…" if trimmed else ""


def _avatar_image(
    avatar_path: Path | None,
    *,
    diameter: int,
    fallback_letter: str,
    font_path: Path,
):  # noqa: ANN201 - PIL image
    """Circular profile picture, or a lettered placeholder when none is set."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    size = diameter * _SUPERSAMPLE
    source = None
    if avatar_path is not None:
        try:
            with Image.open(avatar_path) as handle:
                source = handle.convert("RGBA")
        except (OSError, ValueError):
            # A missing or corrupt avatar must not fail the whole export; fall
            # back to the lettered placeholder.
            source = None

    if source is None:
        base = Image.new("RGBA", (size, size), _AVATAR_FALLBACK_BG)
        if fallback_letter:
            letter_font = ImageFont.truetype(str(font_path), int(size * 0.44))
            ImageDraw.Draw(base).text(
                (size / 2, size / 2),
                fallback_letter,
                font=letter_font,
                fill=(235, 235, 235, 255),
                anchor="mm",
            )
    else:
        base = ImageOps.fit(source, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    base.putalpha(mask)
    return base.resize((diameter, diameter), Image.LANCZOS)


def _verified_badge_image(size: int):  # noqa: ANN201 - PIL image
    """The gold scalloped verified seal with a white check."""
    from PIL import Image, ImageDraw

    scaled = size * _SUPERSAMPLE
    image = Image.new("RGBA", (scaled, scaled), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = scaled / 2
    # Eight overlapping lobes on a ring plus a solid core give the scalloped
    # burst outline without hand-plotting a 16-point star polygon.
    lobe_radius = scaled * 0.155
    ring_radius = scaled * 0.335
    for index in range(8):
        angle = math.tau * index / 8
        lobe_x = center + math.cos(angle) * ring_radius
        lobe_y = center + math.sin(angle) * ring_radius
        draw.ellipse(
            (
                lobe_x - lobe_radius,
                lobe_y - lobe_radius,
                lobe_x + lobe_radius,
                lobe_y + lobe_radius,
            ),
            fill=_VERIFIED_BLUE,
        )
    core_radius = scaled * 0.375
    draw.ellipse(
        (
            center - core_radius,
            center - core_radius,
            center + core_radius,
            center + core_radius,
        ),
        fill=_VERIFIED_BLUE,
    )
    check = [
        (center - scaled * 0.155, center + scaled * 0.005),
        (center - scaled * 0.045, center + scaled * 0.115),
        (center + scaled * 0.165, center - scaled * 0.115),
    ]
    draw.line(check, fill=(255, 255, 255, 255), width=max(2, int(scaled * 0.075)), joint="curve")
    return image.resize((size, size), Image.LANCZOS)
