"""Génère assets/icon.png (256 px) et assets/icon.ico (multi-tailles) avec PIL.

Usage : python assets/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SS = 4                      # sur-échantillonnage pour des bords lisses
TOP, BOTTOM = (91, 140, 255), (139, 92, 246)   # dégradé bleu -> violet
BARS = (0.30, 0.55, 0.85, 0.55, 0.30)          # hauteurs relatives des barres de voix


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            px[x, y] = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,)
    return img


def render(size: int = 256, bg: tuple[int, int, int] | None = None) -> Image.Image:
    """Icône carrée arrondie avec des barres de voix. ``bg`` : couleur unie (icône de la zone
    de notification), sinon dégradé."""
    s = size * SS
    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.23), fill=255)
    fill = Image.new("RGBA", (s, s), bg + (255,)) if bg else _gradient(s)
    base.paste(fill, (0, 0), mask)
    d = ImageDraw.Draw(base)
    n = len(BARS)
    bar_w = s * 0.095
    gap = s * 0.062
    total = n * bar_w + (n - 1) * gap
    x = (s - total) / 2
    for h in BARS:
        bh = s * 0.56 * h
        y0 = (s - bh) / 2
        d.rounded_rectangle((x, y0, x + bar_w, y0 + bh), radius=bar_w / 2, fill=(255, 255, 255, 255))
        x += bar_w + gap
    return base.resize((size, size), Image.LANCZOS)


def main() -> None:
    big = render(256)
    big.save(HERE / "icon.png")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    big.save(HERE / "icon.ico", format="ICO", sizes=sizes)
    print("OK :", HERE / "icon.png", HERE / "icon.ico")


if __name__ == "__main__":
    main()
