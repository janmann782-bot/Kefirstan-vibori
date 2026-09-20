from __future__ import annotations

import os
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import MAP_PATH, PARTIES, REGIONS, ROOT_DIR

OUTPUT_PATH = str(ROOT_DIR / "live_map.png")


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


def _text_with_shadow(draw: ImageDraw.ImageDraw, xy, text: str, font, fill=(255,255,255), anchor="mm"):
    x, y = xy
    for dx, dy in ((-2,0),(2,0),(0,-2),(0,2),(-2,-2),(2,2),(-2,2),(2,-2)):
        draw.multiline_text((x+dx, y+dy), text, font=font, fill=(20,22,24), anchor=anchor, align="center", spacing=2)
    draw.multiline_text((x,y), text, font=font, fill=fill, anchor=anchor, align="center", spacing=2)


def render_map(snapshot: dict, output_path: str = OUTPUT_PATH) -> str:
    img = Image.open(MAP_PATH).convert("RGB")
    px = img.load()
    source_to_region = {REGIONS[r]["source_color"]: r for r in REGIONS}

    # Перекрашиваем только 4 исходных цвета автономий. Текст, фон и границы не трогаем.
    winner_colors = {}
    for r in ("KAR", "TAR", "MAR"):
        p = snapshot["winners"][r]
        winner_colors[r] = PARTIES[p].color
    winner_colors["YAR"] = (52, 57, 60)

    w, h = img.size
    for y in range(h):
        for x in range(w):
            reg = source_to_region.get(px[x, y])
            if reg:
                px[x, y] = winner_colors[reg]

    draw = ImageDraw.Draw(img)
    font_big = _font(34, bold=True)
    font_small = _font(27, bold=True)

    for r in ("KAR", "TAR", "MAR"):
        party = snapshot["winners"][r]
        share = snapshot["shares"][r][party] * 100
        abbr = REGIONS[r]["abbr"]
        label = f"{abbr}\n{PARTIES[party].name} {share:.1f}%"
        _text_with_shadow(draw, REGIONS[r]["label_xy"], label, font_small)

    _text_with_shadow(draw, REGIONS["YAR"]["label_xy"], "ЙАР\nN/D", font_big, fill=(210,210,210))

    img.save(output_path, optimize=True)
    return output_path
