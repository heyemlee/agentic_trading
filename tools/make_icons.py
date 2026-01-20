"""Generate the console's home-screen icons, with no image library.

An icon is the only part of this system a person sees before it has said anything, so it is
worth being deliberate about: a dark ground, and the drawdown ladder that governs every
decision this thing makes, drawn as four rising bars with the top one held back. It is drawn
here rather than committed as opaque binary so it can be read, reviewed and changed like the
rest of the code.

    uv run python tools/make_icons.py

PNG is written by hand because the alternative is a dependency for four flat images. The format
is simple enough: a signature, a header chunk, zlib-compressed scanlines, and an end chunk.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

#: Matches the console's own palette (ui/assets/console.css).
INK = (17, 19, 24)          # background
PAPER = (247, 247, 245)     # the bars
ACCENT = (46, 139, 87)      # the bar that is still climbing

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


def draw(size: int, *, maskable: bool) -> list[list[tuple[int, int, int]]]:
    """Four bars on a dark ground: the ladder, with its top step not yet taken.

    A maskable icon is cropped to whatever shape the platform likes, so its content is kept
    inside the middle 80% and the ground is drawn edge to edge.
    """
    pixels = [[INK] * size for _ in range(size)]
    inset = size * 0.16 if maskable else size * 0.22
    field = size - 2 * inset
    gap = field * 0.09
    bar = (field - 3 * gap) / 4
    heights = (0.34, 0.55, 0.78, 0.52)          # the fourth is the step held back

    for index, fraction in enumerate(heights):
        left = int(inset + index * (bar + gap))
        right = int(left + bar)
        top = int(inset + field * (1 - fraction))
        bottom = int(inset + field)
        colour = ACCENT if index == 3 else PAPER
        for y in range(max(top, 0), min(bottom, size)):
            for x in range(max(left, 0), min(right, size)):
                pixels[y][x] = colour
    return pixels


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "ui" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        write_png(out / name, draw(size, maskable="maskable" in name))
        print(f"wrote {out / name} ({size}x{size})")


if __name__ == "__main__":
    main()
