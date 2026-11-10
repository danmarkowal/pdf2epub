"""A plain typographic cover for books whose PDF has no cover picture.

Reading apps show the cover in their library, and stores expect one, so an
editor would never ship a book without it. The design is deliberately
classic: a dark ground with a thin inset frame, the title in large serif
type in the upper half, and the author's name in spaced capitals below.
"""
import io
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1600, 2400  # the common 2:3 ebook cover shape
BACKGROUND = (35, 38, 43)
INK = (239, 230, 210)
ACCENT = (184, 155, 94)
MARGIN = 150

# Serif fonts to look for, in order of preference (macOS, Linux, Windows)
SERIF_FONTS = [
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/Library/Fonts/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
]


def _font(size: int):
    for path in SERIF_FONTS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)  # Pillow's own font, if no serif is installed


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and draw.textlength(candidate, font=font) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + ([line] if line else [])


def _draw_centred(draw, lines: list[str], font, top: int, line_height: int, spacing: int = 0):
    for n, line in enumerate(lines):
        if spacing:
            width = sum(draw.textlength(c, font=font) for c in line) + spacing * (len(line) - 1)
            x = (WIDTH - width) / 2
            for char in line:
                draw.text((x, top + n * line_height), char, font=font, fill=INK)
                x += draw.textlength(char, font=font) + spacing
        else:
            width = draw.textlength(line, font=font)
            draw.text(((WIDTH - width) / 2, top + n * line_height), line, font=font, fill=INK)


def typographic_cover(title: str, author: Optional[str]) -> tuple[bytes, tuple[int, int]]:
    """Render a cover; returns JPEG bytes and the image size."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    inset = MARGIN // 2
    draw.rectangle([inset, inset, WIDTH - inset, HEIGHT - inset], outline=ACCENT, width=4)
    draw.rectangle([inset + 18, inset + 18, WIDTH - inset - 18, HEIGHT - inset - 18], outline=ACCENT, width=1)

    # The largest title size that fits in four lines
    max_width = WIDTH - 2 * MARGIN - 60
    for size in range(190, 70, -10):
        font = _font(size)
        lines = _wrap(draw, title, font, max_width)
        if len(lines) <= 4 and all(draw.textlength(line, font=font) <= max_width for line in lines):
            break
    line_height = int(size * 1.22)
    block = line_height * len(lines)
    top = int(HEIGHT * 0.38 - block / 2)
    _draw_centred(draw, lines, font, top, line_height)

    rule_y = top + block + 90
    draw.line([(WIDTH / 2 - 160, rule_y), (WIDTH / 2 + 160, rule_y)], fill=ACCENT, width=3)

    if author:
        author_font = _font(64)
        author_lines = _wrap(draw, author.upper(), author_font, max_width - 200)
        _draw_centred(draw, author_lines, author_font, int(HEIGHT * 0.78), 96, spacing=8)

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90, optimize=True)
    return buffer.getvalue(), image.size
