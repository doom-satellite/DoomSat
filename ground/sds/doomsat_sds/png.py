"""A small RGB canvas that writes PNG with the standard library alone.

The path images are drawn here rather than with Pillow for two reasons: the product tests run in ground/.venv,
which has no Pillow, and the output must be byte-identical for identical input so that a product's checksum
means something (same telemetry, same algorithm version, same bytes).
"""
from __future__ import annotations

import struct
import zlib

# 5x7 glyphs, rows top to bottom. Lowercase is drawn as uppercase; anything missing draws as a space.
_FONT_ROWS = {
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.", "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####", "3": "##### ...#. ..#.. ...#. ....# #...# .###.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.", "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.", "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.", "9": ".###. #...# #...# .#### ....# ...#. .##..",
    "A": ".###. #...# #...# ##### #...# #...# #...#", "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.", "D": "###.. #..#. #...# #...# #...# #..#. ###..",
    "E": "##### #.... #.... ####. #.... #.... #####", "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .####", "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.", "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#", "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#", "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.", "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#", "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.", "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.", "V": "#...# #...# #...# #...# #...# .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# #.#.# .#.#.", "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..", "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    " ": "..... ..... ..... ..... ..... ..... .....", "-": "..... ..... ..... ##### ..... ..... .....",
    ":": "..... ..#.. ..#.. ..... ..#.. ..#.. .....", ".": "..... ..... ..... ..... ..... .##.. .##..",
    "/": "..... ....# ...#. ..#.. .#... #.... .....", "_": "..... ..... ..... ..... ..... ..... #####",
    "(": "...#. ..#.. .#... .#... .#... ..#.. ...#.", ")": ".#... ..#.. ...#. ...#. ...#. ..#.. .#...",
    "=": "..... ..... ##### ..... ##### ..... .....", "%": "##... ##..# ...#. ..#.. .#... #..## ...##",
    "+": "..... ..#.. ..#.. ##### ..#.. ..#.. .....", ",": "..... ..... ..... ..... .##.. ..#.. .#...",
    "#": ".#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.", "@": ".###. #...# #.### #.#.# #.### #.... .###.",
    ">": ".#... ..#.. ...#. ....# ...#. ..#.. .#...", "<": "...#. ..#.. .#... #.... .#... ..#.. ...#.",
}
FONT = {ch: [[c == "#" for c in row] for row in rows.split()] for ch, rows in _FONT_ROWS.items()}


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


class Canvas:
    def __init__(self, width: int, height: int, background: str = "#000000"):
        self.w, self.h = width, height
        self.px = bytearray(bytes(hex_rgb(background)) * (width * height))

    def set(self, x: int, y: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = 3 * (y * self.w + x)
            self.px[i:i + 3] = bytes(rgb)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: str) -> None:
        rgb = hex_rgb(color)
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                self.set(x, y, rgb)

    def disc(self, cx: float, cy: float, r: float, color: str) -> None:
        rgb = hex_rgb(color)
        ri = int(r) + 1
        for dy in range(-ri, ri + 1):
            for dx in range(-ri, ri + 1):
                if dx * dx + dy * dy <= r * r:
                    self.set(int(round(cx)) + dx, int(round(cy)) + dy, rgb)

    def line(self, x0: float, y0: float, x1: float, y1: float, color: str | tuple, width: int = 1) -> None:
        """Bresenham, thickened with a square brush."""
        rgb = hex_rgb(color) if isinstance(color, str) else color
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        lo, hi = -((width - 1) // 2), width // 2
        while True:
            for oy in range(lo, hi + 1):
                for ox in range(lo, hi + 1):
                    self.set(x0 + ox, y0 + oy, rgb)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def text(self, x: int, y: int, s: str, color: str = "#ffffff", scale: int = 1) -> int:
        """Draw `s` with the 5x7 font; returns the x after the last glyph."""
        rgb = hex_rgb(color)
        for ch in s:
            glyph = FONT.get(ch.upper(), FONT[" "])
            for gy, row in enumerate(glyph):
                for gx, on in enumerate(row):
                    if on:
                        for oy in range(scale):
                            for ox in range(scale):
                                self.set(x + gx * scale + ox, y + gy * scale + oy, rgb)
            x += 6 * scale
        return x

    def to_png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[3 * y * self.w:3 * (y + 1) * self.w]) for y in range(self.h))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's header, for tests and sanity checks."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])
