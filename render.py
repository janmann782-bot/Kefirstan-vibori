from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import (
    MAP_FULL_COLOR_MARGIN_PP,
    MAP_MIN_PARTY_STRENGTH,
    MAP_NEUTRAL_COLOR,
    MAP_PATH,
    PARTIES,
    REGIONS,
    ROOT_DIR,
)

OUTPUT_PATH = str(ROOT_DIR / "live_map.png")


def _font(size: int, bold: bool = False):
    # На обычном Linux/Bothost используется DejaVu Sans.
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


def _text_with_shadow(draw: ImageDraw.ImageDraw, xy, text: str, font, fill=(255, 255, 255), anchor="mm"):
    x, y = xy
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2), (-2, 2), (2, -2)):
        draw.text((x + dx, y + dy), text, font=font, fill=(18, 20, 22), anchor=anchor)
    draw.text((x, y), text, font=font, fill=fill, anchor=anchor)


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] * (1.0 - t) + b[i] * t) for i in range(3))


def winner_display_color(shares: dict[str, float], winner: str) -> tuple[int, int, int]:
    """Цвет региона = цвет партии, но уверенность победы задает насыщенность.

    При плотном результате вроде 49% против 40% остается хорошо виден цвет ЛПК,
    но он заметно серее. При разгроме в 25-30 п.п. цвет почти/полностью партийный.
    """
    ordered = sorted(shares, key=lambda p: shares[p], reverse=True)
    first = shares[winner]
    second = shares[ordered[1]] if len(ordered) > 1 else 0.0
    margin_pp = max(0.0, (first - second) * 100.0)

    strength = MAP_MIN_PARTY_STRENGTH + (1.0 - MAP_MIN_PARTY_STRENGTH) * min(
        1.0, margin_pp / MAP_FULL_COLOR_MARGIN_PP
    )
    return _blend(MAP_NEUTRAL_COLOR, PARTIES[winner].color, strength)


def render_map(snapshot: dict, output_path: str = OUTPUT_PATH) -> str:
    img = Image.open(MAP_PATH).convert("RGB")
    px = img.load()
    source_to_region = {REGIONS[r]["source_color"]: r for r in REGIONS}

    winner_colors: dict[str, tuple[int, int, int]] = {}
    margins: dict[str, float] = {}
    for r in ("KAR", "TAR", "MAR"):
        shares = snapshot["shares"][r]
        ordered = sorted(PARTIES, key=lambda p: shares[p], reverse=True)
        winner = ordered[0]
        runner = ordered[1]
        winner_colors[r] = winner_display_color(shares, winner)
        margins[r] = max(0.0, (shares[winner] - shares[runner]) * 100.0)
    winner_colors["YAR"] = (52, 57, 60)

    w, h = img.size
    for y in range(h):
        for x in range(w):
            reg = source_to_region.get(px[x, y])
            if reg:
                px[x, y] = winner_colors[reg]

    draw = ImageDraw.Draw(img)
    font_region = _font(31, bold=True)
    font_party = _font(27, bold=True)
    font_margin = _font(19, bold=False)
    font_yar = _font(34, bold=True)

    for r in ("KAR", "TAR", "MAR"):
        party = snapshot["winners"][r]
        share = snapshot["shares"][r][party] * 100
        x, y = REGIONS[r]["label_xy"]
        tone = winner_colors[r]

        _text_with_shadow(draw, (x, y - 31), REGIONS[r]["abbr"], font_region, fill=(245, 245, 245))
        _text_with_shadow(draw, (x, y + 4), f"{PARTIES[party].name} {share:.1f}%", font_party, fill=tone)
        _text_with_shadow(draw, (x, y + 35), f"+{margins[r]:.1f} п.п.", font_margin, fill=(205, 208, 210))

    _text_with_shadow(draw, REGIONS["YAR"]["label_xy"], "ЙАР N/D", font_yar, fill=(210, 210, 210))

    img.save(output_path, optimize=True)
    return output_path
