from __future__ import annotations

import glob
import os
import urllib.request
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

FONT_REGULAR_CACHE = ROOT_DIR / "NotoSans-Regular.ttf"
FONT_BOLD_CACHE = ROOT_DIR / "NotoSans-Bold.ttf"

FONT_URLS = {
    False: "https://raw.githubusercontent.com/notofonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
    True: "https://raw.githubusercontent.com/notofonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf",
}


def _download_font(target: Path, bold: bool) -> Path:
    req = urllib.request.Request(
        FONT_URLS[bold],
        headers={"User-Agent": "Kefirstan-Elections-2059/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read()
    if len(data) < 50_000:
        raise RuntimeError("Скачанный файл шрифта слишком маленький")
    target.write_bytes(data)
    return target


def _system_font_candidates(bold: bool) -> list[str]:
    names = [
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf",
        "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf",
    ]

    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    patterns = [
        "/usr/share/fonts/**/*.ttf",
        "/usr/local/share/fonts/**/*.ttf",
        str(Path.home() / ".fonts/**/*.ttf"),
    ]
    wanted = (
        ("dejavusans-bold", "notosans-bold", "liberationsans-bold")
        if bold
        else ("dejavusans.ttf", "notosans-regular", "liberationsans-regular")
    )

    for pattern in patterns:
        for p in glob.glob(pattern, recursive=True):
            low = os.path.basename(p).lower()
            if any(token in low for token in wanted):
                paths.append(p)

    return names + paths


def _font(size: int, bold: bool = False):
    env_name = "FONT_PATH_BOLD" if bold else "FONT_PATH"
    env_font = os.getenv(env_name, "").strip()
    if env_font:
        try:
            return ImageFont.truetype(env_font, size=size)
        except OSError:
            pass

    for candidate in _system_font_candidates(bold):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue

    cache = FONT_BOLD_CACHE if bold else FONT_REGULAR_CACHE
    if not cache.exists():
        try:
            _download_font(cache, bold)
        except Exception as exc:
            raise RuntimeError(
                "Не найден кириллический шрифт и не удалось скачать Noto Sans. "
                "Укажи FONT_PATH и FONT_PATH_BOLD."
            ) from exc

    return ImageFont.truetype(str(cache), size=size)


def _text_with_shadow(
    draw: ImageDraw.ImageDraw,
    xy,
    text: str,
    font,
    fill=(255, 255, 255, 255),
    anchor="mm",
    shadow=3,
):
    x, y = xy
    for dx, dy in (
        (-shadow, 0), (shadow, 0), (0, -shadow), (0, shadow),
        (-shadow, -shadow), (shadow, shadow),
        (-shadow, shadow), (shadow, -shadow),
    ):
        draw.text((x + dx, y + dy), text, font=font, fill=(15, 17, 19, 255), anchor=anchor)
    draw.text((x, y), text, font=font, fill=fill, anchor=anchor)


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] * (1.0 - t) + b[i] * t) for i in range(3))


def winner_display_color(shares: dict[str, float], winner: str) -> tuple[int, int, int]:
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

    draw = ImageDraw.Draw(img, "RGBA")

    font_region = _font(52, bold=True)
    font_party = _font(43, bold=True)
    font_margin = _font(31, bold=True)
    font_yar = _font(46, bold=True)

    for r in ("KAR", "TAR", "MAR"):
        party = snapshot["winners"][r]
        share = snapshot["shares"][r][party] * 100
        x, y = REGIONS[r]["label_xy"]
        tone = winner_colors[r]

        draw.rounded_rectangle(
            (x - 175, y - 82, x + 175, y + 76),
            radius=24,
            fill=(22, 25, 28, 170),
            outline=(255, 255, 255, 55),
            width=2,
        )

        _text_with_shadow(draw, (x, y - 46), REGIONS[r]["abbr"], font_region)
        _text_with_shadow(
            draw, (x, y + 10),
            f"{PARTIES[party].name} {share:.1f}%",
            font_party,
            fill=(*tone, 255),
        )
        _text_with_shadow(
            draw, (x, y + 52),
            f"+{margins[r]:.1f} п.п.",
            font_margin,
            fill=(220, 223, 226, 255),
            shadow=2,
        )

    x, y = REGIONS["YAR"]["label_xy"]
    draw.rounded_rectangle(
        (x - 165, y - 48, x + 165, y + 48),
        radius=22,
        fill=(22, 25, 28, 170),
        outline=(255, 255, 255, 45),
        width=2,
    )
    _text_with_shadow(
        draw, (x, y), "ЙАР N/D", font_yar,
        fill=(225, 225, 225, 255),
    )

    img.save(output_path, optimize=True)
    return output_path
