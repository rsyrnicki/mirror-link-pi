"""A tiny palette-indexed framebuffer with a built-in 5×7 font.

The VNC server needs something to show, and the Pi has no X server or packages we
can rely on offline. So the "app" is drawn directly into this canvas.

Pixels are palette indices (one byte each). That makes conversion into whatever
pixel format the VNC client asks for fast in pure Python: one ``bytes.translate``
per output byte lane, then interleaving via extended-slice assignment (both run
in C). See ``PixelEncoder``.
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass

# ---------- palette ----------

PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),         # 0 black
    (16, 32, 72),      # 1 background navy
    (245, 245, 245),   # 2 white
    (28, 90, 168),     # 3 header blue
    (255, 0, 0),       # 4 red
    (0, 255, 0),       # 5 green
    (0, 0, 255),       # 6 blue
    (255, 255, 0),     # 7 yellow
    (0, 255, 255),     # 8 cyan
    (255, 0, 255),     # 9 magenta
    (255, 160, 0),     # 10 orange
    (128, 128, 128),   # 11 grey
    (40, 180, 90),     # 12 success green
    (200, 40, 40),     # 13 alert red
]
BLACK, BG, WHITE, HEADER, RED, GREEN, BLUE, YELLOW, CYAN, MAGENTA, ORANGE, GREY, OK, ALERT = (
    range(len(PALETTE)))

# ---------- font ----------

_GLYPHS_SRC = {
    "A": ".###. #...# #...# ##### #...# #...# #...#",
    "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.",
    "D": "####. #...# #...# #...# #...# #...# ####.",
    "E": "##### #.... #.... ####. #.... #.... #####",
    "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .####",
    "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.",
    "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#",
    "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.",
    "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.",
    "V": "#...# #...# #...# #...# #...# .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# #.#.# .#.#.",
    "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",
    "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.",
    "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####",
    "3": "##### ...#. ..#.. ...#. ....# #...# .###.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.",
    "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.",
    "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.",
    "9": ".###. #...# #...# .#### ....# ...#. .##..",
    " ": "..... ..... ..... ..... ..... ..... .....",
    ".": "..... ..... ..... ..... ..... .##.. .##..",
    ",": "..... ..... ..... ..... .##.. ..#.. .#...",
    ":": "..... .##.. .##.. ..... .##.. .##.. .....",
    "-": "..... ..... ..... ##### ..... ..... .....",
    "_": "..... ..... ..... ..... ..... ..... #####",
    "/": "....# ....# ...#. ..#.. .#... #.... #....",
    "#": ".#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.",
    "(": "...#. ..#.. .#... .#... .#... ..#.. ...#.",
    ")": ".#... ..#.. ...#. ...#. ...#. ..#.. .#...",
    "=": "..... ..... ##### ..... ##### ..... .....",
    "+": "..... ..#.. ..#.. ##### ..#.. ..#.. .....",
    "%": "##... ##..# ...#. ..#.. .#... #..## ...##",
    "!": "..#.. ..#.. ..#.. ..#.. ..#.. ..... ..#..",
    "?": ".###. #...# ....# ...#. ..#.. ..... ..#..",
    "'": "..#.. ..#.. .#... ..... ..... ..... .....",
    '"': ".#.#. .#.#. ..... ..... ..... ..... .....",
    ">": ".#... ..#.. ...#. ....# ...#. ..#.. .#...",
    "<": "...#. ..#.. .#... #.... .#... ..#.. ...#.",
    "*": "..... ..#.. #.#.# .###. #.#.# ..#.. .....",
    "[": ".###. .#... .#... .#... .#... .#... .###.",
    "]": ".###. ...#. ...#. ...#. ...#. ...#. .###.",
    "|": "..#.. ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
}

GLYPH_W, GLYPH_H = 5, 7
ADVANCE = GLYPH_W + 1


def _compile_glyphs() -> dict[str, list[tuple[int, int]]]:
    out = {}
    for ch, src in _GLYPHS_SRC.items():
        rows = src.split()
        assert len(rows) == GLYPH_H and all(len(r) == GLYPH_W for r in rows), ch
        # Store horizontal runs (y, x_start, length) to keep drawing cheap.
        runs = []
        for y, row in enumerate(rows):
            x = 0
            while x < GLYPH_W:
                if row[x] == "#":
                    start = x
                    while x < GLYPH_W and row[x] == "#":
                        x += 1
                    runs.append((y, start, x - start))
                else:
                    x += 1
        out[ch] = runs
    return out


GLYPHS = _compile_glyphs()


def text_width(text: str, scale: int) -> int:
    return len(text) * ADVANCE * scale - scale if text else 0


# ---------- pixel formats ----------

@dataclass(frozen=True)
class PixelFormat:
    bits_per_pixel: int
    depth: int
    big_endian: bool
    true_colour: bool
    red_max: int
    green_max: int
    blue_max: int
    red_shift: int
    green_shift: int
    blue_shift: int

    _STRUCT = struct.Struct("!BBBBHHHBBB3x")

    def pack(self) -> bytes:
        return self._STRUCT.pack(
            self.bits_per_pixel, self.depth, int(self.big_endian), int(self.true_colour),
            self.red_max, self.green_max, self.blue_max,
            self.red_shift, self.green_shift, self.blue_shift,
        )

    @classmethod
    def unpack(cls, data: bytes) -> PixelFormat:
        bpp, depth, be, tc, rmax, gmax, bmax, rs, gs, bs = cls._STRUCT.unpack(data)
        return cls(bpp, depth, bool(be), bool(tc), rmax, gmax, bmax, rs, gs, bs)

    def describe(self) -> str:
        if not self.true_colour:
            return f"{self.bits_per_pixel}bpp colour-map"
        return (f"{self.bits_per_pixel}bpp depth {self.depth} "
                f"{'BE' if self.big_endian else 'LE'} "
                f"R{self.red_max}<<{self.red_shift} G{self.green_max}<<{self.green_shift} "
                f"B{self.blue_max}<<{self.blue_shift}")


DEFAULT_PIXEL_FORMAT = PixelFormat(32, 24, False, True, 255, 255, 255, 16, 8, 0)


class UnsupportedPixelFormat(ValueError):
    pass


class PixelEncoder:
    """Converts rows of palette indices into the client's pixel format."""

    def __init__(self, pf: PixelFormat, palette: list[tuple[int, int, int]] = PALETTE) -> None:
        if pf.bits_per_pixel not in (8, 16, 32):
            raise UnsupportedPixelFormat(f"bits_per_pixel={pf.bits_per_pixel}")
        self.pf = pf
        self.bytes_per_pixel = pf.bits_per_pixel // 8
        values = []
        for i in range(256):
            r, g, b = palette[i] if i < len(palette) else (0, 0, 0)
            if pf.true_colour:
                v = (((r * pf.red_max + 127) // 255) << pf.red_shift
                     | ((g * pf.green_max + 127) // 255) << pf.green_shift
                     | ((b * pf.blue_max + 127) // 255) << pf.blue_shift)
            else:
                v = i  # colour-map mode: the pixel value *is* the palette index
            v &= (1 << pf.bits_per_pixel) - 1
            values.append(v.to_bytes(self.bytes_per_pixel, "big" if pf.big_endian else "little"))
        self._tables = [bytes(v[k] for v in values) for k in range(self.bytes_per_pixel)]

    def encode_row(self, indices: bytes | bytearray | memoryview) -> bytes:
        if self.bytes_per_pixel == 1:
            return bytes(indices).translate(self._tables[0])
        src = bytes(indices)
        n = len(src)
        bpp = self.bytes_per_pixel
        out = bytearray(n * bpp)
        for k, table in enumerate(self._tables):
            out[k::bpp] = src.translate(table)
        return bytes(out)


def colour_map_entries(palette: list[tuple[int, int, int]] = PALETTE) -> bytes:
    """RFB SetColourMapEntries message covering the palette (for colour-map clients)."""
    msg = bytearray(struct.pack("!BxHH", 1, 0, len(palette)))
    for r, g, b in palette:
        msg += struct.pack("!HHH", r * 257, g * 257, b * 257)
    return bytes(msg)


# ---------- canvas ----------

class Canvas:
    """Palette-indexed framebuffer with dirty-rectangle tracking.

    Drawing happens under ``lock``; call ``commit()`` after a batch to publish the
    changes and wake VNC senders waiting for incremental updates.
    """

    _DIRTY_HISTORY = 128

    def __init__(self, width: int, height: int, fill: int = BG) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray([fill]) * (width * height)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self._pending: tuple[int, int, int, int] | None = None
        self._history: list[tuple[int, tuple[int, int, int, int]]] = []

    # ----- drawing primitives (caller holds no lock; each takes it) -----

    def fill_rect(self, x: int, y: int, w: int, h: int, colour: int) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        run = bytes([colour]) * (x1 - x0)
        with self.lock:
            px, width = self.pixels, self.width
            for row in range(y0, y1):
                o = row * width
                px[o + x0:o + x1] = run
            self._mark(x0, y0, x1 - x0, y1 - y0)

    def text(self, x: int, y: int, text: str, *, scale: int = 2, colour: int = WHITE,
             background: int | None = None) -> int:
        """Draw ``text``; returns its width in pixels. Unknown characters render as '?'."""
        width = text_width(text, scale)
        with self.lock:
            if background is not None:
                self.fill_rect(x - scale, y - scale, width + 2 * scale,
                               GLYPH_H * scale + 2 * scale, background)
            cx = x
            for ch in text.upper():
                for gy, gx, length in GLYPHS.get(ch, GLYPHS["?"]):
                    self.fill_rect(cx + gx * scale, y + gy * scale, length * scale, scale, colour)
                cx += ADVANCE * scale
        return width

    def _mark(self, x: int, y: int, w: int, h: int) -> None:
        if self._pending is None:
            self._pending = (x, y, x + w, y + h)
        else:
            a = self._pending
            self._pending = (min(a[0], x), min(a[1], y), max(a[2], x + w), max(a[3], y + h))

    def commit(self) -> None:
        with self.lock:
            if self._pending is None:
                return
            self.version += 1
            self._history.append((self.version, self._pending))
            del self._history[:-self._DIRTY_HISTORY]
            self._pending = None
            self.changed.notify_all()

    # ----- reading -----

    def changes_since(self, version: int) -> tuple[int, tuple[int, int, int, int] | None]:
        """Return (current_version, bounding box x0,y0,x1,y1 of changes after ``version``).

        If the history no longer reaches back that far, the whole screen is returned.
        """
        with self.lock:
            if version >= self.version:
                return self.version, None
            if not self._history or self._history[0][0] > version + 1:
                return self.version, (0, 0, self.width, self.height)
            box = None
            for v, (x0, y0, x1, y1) in self._history:
                if v <= version:
                    continue
                box = (x0, y0, x1, y1) if box is None else (
                    min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1))
            return self.version, box

    def encode_rect(self, encoder: PixelEncoder, x: int, y: int, w: int, h: int) -> bytes:
        with self.lock:
            px, width = self.pixels, self.width
            rows = [encoder.encode_row(px[r * width + x:r * width + x + w])
                    for r in range(y, y + h)]
        return b"".join(rows)

    def to_png(self) -> bytes:
        """Snapshot as PNG (used by the car simulator and the tests)."""
        from .http_descriptor import encode_png

        with self.lock:
            data = bytes(self.pixels)
        rgb = [bytes(c) for c in PALETTE] + [b"\0\0\0"] * (256 - len(PALETTE))
        rows = bytearray()
        for y in range(self.height):
            rows.append(0)
            rows += b"".join(rgb[i] for i in data[y * self.width:(y + 1) * self.width])
        return encode_png(self.width, self.height, bytes(rows))
