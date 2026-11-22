"""The SDS draws its path images and contact sheets with its own PNG writer (doomsat_sds.png.Canvas), not Pillow.

Two reasons, both load-bearing: the product tests run in ground/.venv, which has no Pillow, and a product's
sha256 only means something if the same drawing gives the same bytes. A writer nobody decodes can emit a file
that looks fine to png_size() and is garbage to a viewer, so these tests decode every image with an
independent reader (chunks, CRCs, inflate, filter bytes) and check the pixels a drawing should have set, plus
the clipping and font rules the path renderer relies on when a marker or label lands at the edge.
"""
import io
import os
import struct
import sys
import unittest
import zlib

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)
from doomsat_sds import png  # noqa: E402

try:
    import PIL.Image
    HAVE_PIL = True
except ImportError:          # ground/.venv has no Pillow; the SDS venv does
    HAVE_PIL = False

SIGNATURE = b"\x89PNG\r\n\x1a\n"
WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def read_chunks(data: bytes) -> list:
    """[(type, body)] in file order, every CRC checked. Raises ValueError on anything malformed."""
    if data[:8] != SIGNATURE:
        raise ValueError("no PNG signature")
    pos, out = 8, []
    while pos < len(data):
        if pos + 12 > len(data):
            raise ValueError("truncated chunk at %d" % pos)
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise ValueError("bad CRC in %r chunk" % kind)
        out.append((kind, body))
        pos += 12 + n
    return out


def decode(data: bytes) -> dict:
    """An 8-bit RGB PNG as {"size", "header", "chunks", "filters", "pixels"}; pixels[y][x] is an (r, g, b)."""
    chunks = read_chunks(data)
    kinds = [k for k, _ in chunks]
    if kinds[0] != b"IHDR" or kinds[-1] != b"IEND":
        raise ValueError("chunk order %r" % kinds)
    w, h, depth, colour, comp, flt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    if (depth, colour) != (8, 2):
        raise ValueError("only 8-bit RGB is decoded here, got depth %d colour type %d" % (depth, colour))
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    stride = 1 + 3 * w
    if len(raw) != h * stride:
        raise ValueError("IDAT inflates to %d bytes, expected %d" % (len(raw), h * stride))
    pixels = [[tuple(raw[y * stride + 1 + 3 * x:y * stride + 4 + 3 * x]) for x in range(w)] for y in range(h)]
    return {"size": (w, h), "header": (depth, colour, comp, flt, interlace), "chunks": kinds,
            "filters": {raw[y * stride] for y in range(h)}, "pixels": pixels, "iend": chunks[-1][1]}


def painted(canvas: png.Canvas, background=BLACK) -> set:
    """{(x, y)} of every pixel that is not the background, read back from the encoded PNG."""
    px = decode(canvas.to_png())["pixels"]
    return {(x, y) for y, row in enumerate(px) for x, v in enumerate(row) if v != background}


class TestEncoding(unittest.TestCase):
    def test_structure_is_a_plain_rgb_png(self):
        d = decode(png.Canvas(7, 5, "#102030").to_png())
        self.assertEqual(d["size"], (7, 5))
        self.assertEqual(d["header"], (8, 2, 0, 0, 0))           # 8-bit, truecolour, deflate, no interlace
        self.assertEqual(d["chunks"][0], b"IHDR")
        self.assertEqual(d["chunks"][-1], b"IEND")
        self.assertIn(b"IDAT", d["chunks"])
        self.assertEqual(d["iend"], b"")
        self.assertEqual(d["filters"], {0}, "every scanline uses filter type 0 (None)")

    def test_background_fills_every_pixel(self):
        d = decode(png.Canvas(9, 4, "#102030").to_png())
        self.assertEqual({v for row in d["pixels"] for v in row}, {(0x10, 0x20, 0x30)})

    def test_decoder_really_checks_crcs(self):
        # Guards this file rather than the writer: a decoder that ignored CRCs would pass anything.
        data = bytearray(png.Canvas(4, 4).to_png())
        data[8 + 8 + 2] ^= 0xFF                                    # a byte inside the IHDR body
        with self.assertRaises(ValueError):
            read_chunks(bytes(data))

    def test_set_writes_exactly_one_pixel(self):
        c = png.Canvas(6, 5)
        c.set(4, 3, (1, 2, 3))
        d = decode(c.to_png())
        self.assertEqual(d["pixels"][3][4], (1, 2, 3))
        self.assertEqual(painted(c), {(4, 3)})


class TestDrawing(unittest.TestCase):
    def test_rect_is_half_open(self):
        c = png.Canvas(6, 5)
        c.rect(1, 1, 4, 3, "#ff0000")
        self.assertEqual(painted(c), {(x, y) for x in range(1, 4) for y in range(1, 3)})
        self.assertEqual(decode(c.to_png())["pixels"][1][1], (255, 0, 0))

    def test_rect_clips_to_the_canvas(self):
        c = png.Canvas(5, 4)
        c.rect(-3, -2, 2, 99, "#00ff00")
        self.assertEqual(painted(c), {(x, y) for x in range(0, 2) for y in range(0, 4)})

    def test_disc(self):
        c = png.Canvas(15, 15)
        c.disc(7, 7, 3, "#00ff00")
        on = painted(c)
        self.assertEqual(decode(c.to_png())["pixels"][7][7], (0, 255, 0))
        for p in [(7, 7), (10, 7), (4, 7), (7, 10), (7, 4), (9, 9), (5, 5)]:
            self.assertIn(p, on)
        for p in [(10, 8), (10, 10), (4, 4), (11, 7), (7, 3)]:
            self.assertNotIn(p, on)
        self.assertEqual(len(on), 29)                              # lattice points with dx*dx + dy*dy <= 9

    def test_disc_rounds_a_fractional_centre(self):
        a, b = png.Canvas(15, 15), png.Canvas(15, 15)
        a.disc(7, 7, 3, "#00ff00")
        b.disc(7.4, 6.6, 3, "#00ff00")
        self.assertEqual(a.to_png(), b.to_png())

    def test_disc_at_the_edge_is_clipped(self):
        c = png.Canvas(10, 10)
        c.disc(0, 9, 4, "#00ff00")                                 # three quarters off the canvas
        on = painted(c)
        self.assertIn((0, 9), on)
        self.assertTrue(all(0 <= x < 10 and 0 <= y < 10 for x, y in on))

    def test_line_endpoints_and_one_pixel_per_step(self):
        c = png.Canvas(10, 8)
        c.line(1, 1, 8, 5, "#ffffff")
        on = painted(c)
        px = decode(c.to_png())["pixels"]
        self.assertEqual(px[1][1], WHITE)
        self.assertEqual(px[5][8], WHITE)
        # Bresenham on an x-major line: exactly one pixel in each column from x0 to x1, each step moving y by 0 or 1.
        self.assertEqual(sorted(x for x, _ in on), list(range(1, 9)))
        ys = [y for _, y in sorted(on)]
        self.assertTrue(all(b - a in (0, 1) for a, b in zip(ys, ys[1:])))

    def test_line_endpoints_steep_and_backwards(self):
        c = png.Canvas(10, 10)
        c.line(7, 8, 2, 1, "#ffffff")                              # y-major, drawn right to left, bottom to top
        on = painted(c)
        self.assertIn((7, 8), on)
        self.assertIn((2, 1), on)
        self.assertEqual(sorted(y for _, y in on), list(range(1, 9)))

    def test_line_of_zero_length_is_one_pixel(self):
        c = png.Canvas(5, 5)
        c.line(2, 3, 2, 3, "#ffffff")
        self.assertEqual(painted(c), {(2, 3)})

    def test_line_brush_thickness_equals_width(self):
        for width in (1, 2, 3, 4):
            c = png.Canvas(14, 14)
            c.line(3, 6, 9, 6, "#ffffff", width)
            on = painted(c)
            column = {y for x, y in on if x == 6}
            self.assertEqual(len(column), width, "width %d" % width)
            self.assertIn(6, column)

    def test_line_takes_an_rgb_tuple_and_rounds_float_coordinates(self):
        a, b = png.Canvas(10, 10), png.Canvas(10, 10)
        a.line(1, 2, 8, 6, "#0a141e")
        b.line(1.2, 1.8, 7.6, 6.4, (10, 20, 30))
        self.assertEqual(a.to_png(), b.to_png())

    def test_line_partly_off_the_canvas(self):
        c = png.Canvas(6, 6)
        c.line(-5, 2, 10, 2, "#ffffff", 3)
        self.assertEqual(painted(c), {(x, y) for x in range(6) for y in (1, 2, 3)})

    def test_set_outside_bounds_is_ignored_and_never_wraps(self):
        # The pixel buffer is one flat bytearray: an unchecked (-1, 1) would land on (w-1, 0) and (w, 0) on
        # (0, 1). The path renderer relies on this when a marker or a label runs off the edge.
        c = png.Canvas(4, 3, "#000000")
        before = c.to_png()
        for x, y in [(-1, 0), (-1, 1), (4, 0), (4, 2), (0, -1), (0, 3), (3, 3), (-100, -100), (10 ** 6, 1)]:
            c.set(x, y, (255, 255, 255))
        self.assertEqual(c.to_png(), before)


class TestText(unittest.TestCase):
    def test_returns_the_advanced_x(self):
        c = png.Canvas(200, 40)
        for scale in (1, 2, 3):
            self.assertEqual(c.text(5, 1, "L1 PATH", "#ffffff", scale), 5 + 6 * scale * 7)
        self.assertEqual(c.text(17, 1, ""), 17)

    def test_glyph_pixels(self):
        c = png.Canvas(12, 10)
        c.text(2, 1, "-")                                          # row 3 of the 5x7 glyph is solid
        self.assertEqual(painted(c), {(x, 4) for x in range(2, 7)})
        self.assertEqual(decode(c.to_png())["pixels"][4][2], WHITE)
        c = png.Canvas(12, 16)
        c.text(0, 0, "-", "#ff0000", 2)
        self.assertEqual(painted(c), {(x, y) for x in range(0, 10) for y in (6, 7)})

    def test_unknown_characters_draw_nothing_but_still_advance(self):
        c = png.Canvas(60, 10)
        before = c.to_png()
        self.assertEqual(c.text(3, 1, "~é\t\n☃"), 3 + 6 * 5)
        self.assertEqual(c.to_png(), before)

    def test_lowercase_draws_as_uppercase(self):
        a, b = png.Canvas(80, 10), png.Canvas(80, 10)
        a.text(1, 1, "e0002 died")
        b.text(1, 1, "E0002 DIED")
        self.assertEqual(a.to_png(), b.to_png())
        self.assertTrue(painted(a))

    def test_text_running_off_the_canvas(self):
        c = png.Canvas(20, 8)
        self.assertEqual(c.text(-4, -3, "WWWWWW", "#ffffff", 2), -4 + 72)
        self.assertTrue(painted(c))

    def test_every_glyph_is_5_by_7(self):
        # text() advances 6 px per glyph at scale 1: five columns and a one-pixel gap.
        for ch, glyph in png.FONT.items():
            self.assertEqual(len(glyph), 7, repr(ch))
            self.assertTrue(all(len(row) == 5 for row in glyph), repr(ch))
        self.assertFalse(any(any(row) for row in png.FONT[" "]))


class TestHelpers(unittest.TestCase):
    def test_hex_rgb(self):
        self.assertEqual(png.hex_rgb("#ff8000"), (255, 128, 0))
        self.assertEqual(png.hex_rgb("FF8000"), (255, 128, 0))
        self.assertEqual(png.hex_rgb("#000000"), (0, 0, 0))
        self.assertEqual(png.hex_rgb("#e04040"), (0xE0, 0x40, 0x40))

    def test_png_size(self):
        self.assertEqual(png.png_size(png.Canvas(600, 640).to_png()), (600, 640))
        self.assertEqual(png.png_size(png.Canvas(3, 1).to_png()), (3, 1))

    def test_png_size_rejects_non_png(self):
        for data in (b"", b"GIF89a" + b"\x00" * 20, b"\xff\xd8\xff\xe0" + b"\x00" * 20, b"\x89PNG\r\n"):
            with self.assertRaises(ValueError):
                png.png_size(data)

    def _scene(self, colour="#40d070") -> bytes:
        c = png.Canvas(64, 48, "#101418")
        c.rect(0, 0, 64, 8, "#1c232b")
        c.text(2, 1, "20261004T004647Z-e0002", "#ffffff")
        c.line(5, 40, 58, 12, "#f0b030", 2)
        c.disc(5, 40, 4, colour)
        c.disc(58, 12, 4, "#e04040")
        return c.to_png()

    def test_identical_drawing_gives_identical_bytes(self):
        self.assertEqual(self._scene(), self._scene())
        self.assertNotEqual(self._scene(), self._scene("#40d071"))


@unittest.skipUnless(HAVE_PIL, "Pillow is not installed (ground/.venv); the SDS venv has it")
class TestAgainstPillow(unittest.TestCase):
    def test_pillow_reads_the_same_pixels(self):
        c = png.Canvas(40, 30, "#101418")
        c.rect(3, 3, 12, 9, "#ff0000")
        c.disc(25, 15, 6, "#40d070")
        c.line(0, 29, 39, 0, "#f0b030", 2)
        c.text(2, 20, "OK 42", "#ffffff")
        data = c.to_png()
        with PIL.Image.open(io.BytesIO(data)) as im:
            im.load()
            self.assertEqual(im.format, "PNG")
            self.assertEqual(im.mode, "RGB")
            self.assertEqual(im.size, (40, 30))
            theirs = im.tobytes()                                  # packed RGB, row by row
        ours = bytes(c for row in decode(data)["pixels"] for v in row for c in v)
        self.assertEqual(theirs, ours)


if __name__ == "__main__":
    unittest.main()
