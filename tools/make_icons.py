"""Generate the letter-free interlocking signal mark as SVG and antialiased PNG.

Two offset chevrons form an open link: independent agents working together.
The shared geometry keeps the navigation mark and installed app icons consistent.
Run: uv run python tools/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from itertools import pairwise
from pathlib import Path

INK = (16, 37, 29)
PAPER = (239, 247, 237)
ACCENT = (136, 224, 164)
STROKES = (((29, 55), (49, 35), (61, 47)), ((39, 53), (51, 65), (71, 45)))
RADIUS = 4.5

SIZES = {"icon-180.png": 180, "icon-192.png": 192, "icon-512.png": 512,
         "icon-maskable-512.png": 512}


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def write_png(path: Path, pixels: list[list[tuple[int, int, int]]]) -> None:
    height, width = len(pixels), len(pixels[0])
    raw = b"".join(b"\x00" + bytes(v for pixel in row for v in pixel) for row in pixels)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b""))


def distance_squared(x: float, y: float, a: tuple, b: tuple) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
    return (x - a[0] - t * dx) ** 2 + (y - a[1] - t * dy) ** 2


def draw(size: int, *, maskable: bool) -> list[list[tuple[int, int, int]]]:
    """Keep the entire mark inside the maskable safe circle; smooth stroke edges."""
    pixels = [[INK] * size for _ in range(size)]
    # Identical safe geometry for ordinary and maskable platform crops.
    scale = 100 / size
    for y in range(int(size * .29), int(size * .71) + 1):
        for x in range(int(size * .23), int(size * .77) + 1):
            colour = list(INK)
            for points, ink in zip(STROKES, (PAPER, ACCENT), strict=True):
                distance = min(distance_squared((x + .5) * scale, (y + .5) * scale, a, b)
                               for a, b in pairwise(points)) ** .5
                coverage = max(0, min(1, (RADIUS - distance) / scale + .5))
                colour = [round(c * (1 - coverage) + v * coverage)
                          for c, v in zip(colour, ink, strict=True)]
            pixels[y][x] = tuple(colour)
    return pixels


def svg() -> str:
    def hex_colour(colour: tuple) -> str:
        return "#" + "".join(f"{v:02x}" for v in colour)

    paths = "".join(
        f'<polyline points="{" ".join(f"{x},{y}" for x, y in points)}" '
        f'stroke="{hex_colour(colour)}"/>'
        for points, colour in zip(STROKES, (PAPER, ACCENT), strict=True))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            f'<rect width="100" height="100" rx="24" fill="{hex_colour(INK)}"/>'
            f'<g fill="none" stroke-width="{RADIUS * 2}" stroke-linecap="round" '
            f'stroke-linejoin="round">{paths}</g></svg>\n')


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "ui" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    (out / "mark.svg").write_text(svg())
    for name, size in SIZES.items():
        write_png(out / name, draw(size, maskable="maskable" in name))
        print(f"wrote {out / name} ({size}x{size})")


if __name__ == "__main__":
    main()
