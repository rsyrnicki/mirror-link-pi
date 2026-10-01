"""True-colour video frames for the VNC server, and switching what the car sees.

The status screen is a palette-indexed ``Canvas``. Phone mirroring produces decoded
video instead: whole RGB565 frames. ``VideoFrame`` stores those and offers the same
interface the RFB sender uses on a Canvas (``version``, ``changed``,
``changes_since``, ``encode_rect``); ``DisplaySwitch`` picks which of the two the car
currently sees, and ``InputRouter`` sends touches/keys to the phone while it is shown.

Everything here is pure standard library. RGB565 → client pixel format conversion
runs through ``bytes.translate`` and big-int OR (both in C), so it is fast enough on a
Pi Zero 2 W; the car asks for RGB565 little-endian, which is a plain copy.
"""

from __future__ import annotations

import threading
from typing import Protocol

from .canvas import Canvas, PixelEncoder, PixelFormat

# Bits of the combined DisplaySwitch version reserved for the source's own version.
_GEN_SHIFT = 40
_VER_MASK = (1 << _GEN_SHIFT) - 1


def is_rgb565le(pf: PixelFormat) -> bool:
    return (pf.true_colour and pf.bits_per_pixel == 16 and not pf.big_endian
            and (pf.red_max, pf.green_max, pf.blue_max) == (31, 63, 31)
            and (pf.red_shift, pf.green_shift, pf.blue_shift) == (11, 5, 0))


def _bits(maximum: int) -> int:
    return max(0, maximum.bit_length())


class Rgb565Converter:
    """Convert RGB565 little-endian pixels to any true-colour RFB pixel format.

    Each output byte lane is ``table_hi[hi_byte] | table_lo[lo_byte]``: red comes from
    the high byte, blue from the low byte and green straddles both, so channel scaling
    is done with shifts (which distribute over OR). Both halves are computed with
    ``bytes.translate`` and OR-ed as big integers.
    """

    def __init__(self, pf: PixelFormat) -> None:
        if not pf.true_colour or pf.bits_per_pixel not in (8, 16, 32):
            raise ValueError(f"unsupported pixel format for video: {pf.describe()}")
        self.pf = pf
        self.bpp = pf.bits_per_pixel // 8
        self.identity = is_rgb565le(pf)

        def scale(value: int, in_bits: int, out_max: int) -> int:
            shift = _bits(out_max) - in_bits
            return value << shift if shift >= 0 else value >> -shift

        hi_vals, lo_vals = [], []
        for byte in range(256):
            # high byte: RRRRRGGG (red 5 bits, green bits 5..3)
            r = byte >> 3
            g_hi = (byte & 0x07) << 3
            hi_vals.append(scale(r, 5, pf.red_max) << pf.red_shift
                           | scale(g_hi, 6, pf.green_max) << pf.green_shift)
            # low byte: GGGBBBBB (green bits 2..0, blue 5 bits)
            g_lo = byte >> 5
            b = byte & 0x1F
            lo_vals.append(scale(g_lo, 6, pf.green_max) << pf.green_shift
                           | scale(b, 5, pf.blue_max) << pf.blue_shift)
        order = "big" if pf.big_endian else "little"
        mask = (1 << pf.bits_per_pixel) - 1

        def lanes(values: list[int]) -> list[bytes]:
            encoded = [(v & mask).to_bytes(self.bpp, order) for v in values]
            return [bytes(e[k] for e in encoded) for k in range(self.bpp)]

        self._hi = lanes(hi_vals)
        self._lo = lanes(lo_vals)

    def convert(self, rgb565le: bytes) -> bytes:
        if self.identity:
            return rgb565le
        lo, hi = rgb565le[0::2], rgb565le[1::2]
        n = len(lo)
        if self.bpp == 1:
            return (int.from_bytes(hi.translate(self._hi[0]), "little")
                    | int.from_bytes(lo.translate(self._lo[0]), "little")).to_bytes(n, "little")
        out = bytearray(n * self.bpp)
        for k in range(self.bpp):
            lane = (int.from_bytes(hi.translate(self._hi[k]), "little")
                    | int.from_bytes(lo.translate(self._lo[k]), "little"))
            out[k::self.bpp] = lane.to_bytes(n, "little")
        return bytes(out)


class VideoFrame:
    """The latest decoded video frame (RGB565 LE) with dirty-row tracking."""

    _HISTORY = 64

    def __init__(self, width: int, height: int, *,
                 changed: threading.Condition | None = None) -> None:
        self.width = width
        self.height = height
        self.changed = changed or threading.Condition(threading.RLock())
        self.pixels = bytearray(width * height * 2)
        self.version = 0
        self.frames = 0
        self._history: list[tuple[int, tuple[int, int, int, int]]] = []
        self._converters: dict[PixelFormat, Rgb565Converter] = {}

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 2

    def update(self, frame: bytes) -> bool:
        """Publish a new frame; returns False if nothing changed."""
        if len(frame) != self.frame_bytes:
            raise ValueError(f"frame is {len(frame)} bytes, want {self.frame_bytes}")
        stride = self.width * 2
        with self.changed:
            old = self.pixels
            first = last = -1
            for row in range(self.height):
                o = row * stride
                if old[o:o + stride] != frame[o:o + stride]:
                    first = row
                    break
            if first < 0:
                self.frames += 1
                return False
            for row in range(self.height - 1, first - 1, -1):
                o = row * stride
                if old[o:o + stride] != frame[o:o + stride]:
                    last = row
                    break
            start, end = first * stride, (last + 1) * stride
            old[start:end] = frame[start:end]
            self.version += 1
            self.frames += 1
            self._history.append((self.version, (0, first, self.width, last + 1)))
            del self._history[:-self._HISTORY]
            self.changed.notify_all()
        return True

    def changes_since(self, version: int) -> tuple[int, tuple[int, int, int, int] | None]:
        with self.changed:
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
        pf = encoder.pf
        conv = self._converters.get(pf)
        if conv is None:
            conv = self._converters[pf] = Rgb565Converter(pf)
        stride = self.width * 2
        with self.changed:
            if x == 0 and w == self.width:
                raw = bytes(self.pixels[y * stride:(y + h) * stride])
            else:
                raw = b"".join(bytes(self.pixels[r * stride + 2 * x:r * stride + 2 * (x + w)])
                               for r in range(y, y + h))
        return conv.convert(raw)

    def to_png(self) -> bytes:
        """PNG of the current frame (for screenshots in desk tests)."""
        from .http_descriptor import encode_png
        conv = Rgb565Converter(PixelFormat(32, 24, False, True, 255, 255, 255, 0, 8, 16))
        with self.changed:
            rgbx = conv.convert(bytes(self.pixels))
        rgb = bytearray(len(rgbx) // 4 * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = rgbx[0::4], rgbx[1::4], rgbx[2::4]
        row = self.width * 3
        rows = b"".join(b"\0" + bytes(rgb[y * row:(y + 1) * row]) for y in range(self.height))
        return encode_png(self.width, self.height, rows)


class _Source(Protocol):
    width: int
    height: int
    version: int

    def changes_since(self, version: int) -> tuple[int, tuple[int, int, int, int] | None]: ...

    def encode_rect(self, encoder: PixelEncoder, x: int, y: int, w: int, h: int) -> bytes: ...


class DisplaySwitch:
    """What the car sees: the status canvas or the phone's video.

    Presents the Canvas interface to the RFB server. Switching sources bumps a
    generation number inside ``version``, so every VNC client gets a full frame.
    """

    def __init__(self, canvas: Canvas) -> None:
        self.canvas = canvas
        self.width, self.height = canvas.width, canvas.height
        self.changed = canvas.changed      # one condition wakes senders for both sources
        self.active: _Source = canvas
        self._gen = 0

    def new_video_frame(self) -> VideoFrame:
        return VideoFrame(self.width, self.height, changed=self.changed)

    @property
    def version(self) -> int:
        return (self._gen << _GEN_SHIFT) | self.active.version

    def show(self, source: _Source) -> None:
        with self.changed:
            if source is self.active:
                return
            self.active = source
            self._gen += 1
            self.changed.notify_all()

    def showing(self, source: _Source) -> bool:
        return self.active is source

    def changes_since(self, version: int) -> tuple[int, tuple[int, int, int, int] | None]:
        with self.changed:
            if version < 0 or version >> _GEN_SHIFT != self._gen:
                return self.version, (0, 0, self.width, self.height)
            inner, box = self.active.changes_since(version & _VER_MASK)
            return (self._gen << _GEN_SHIFT) | inner, box

    def encode_rect(self, encoder: PixelEncoder, x: int, y: int, w: int, h: int) -> bytes:
        return self.active.encode_rect(encoder, x, y, w, h)


class InputSink(Protocol):
    def on_pointer(self, x: int, y: int, buttons: int) -> None: ...

    def on_key(self, keysym: int, down: bool) -> None: ...


class InputRouter:
    """Stands in for the StatusScreen towards the RFB server.

    The status screen always sees input (it echoes it); while the phone is shown,
    touches and keys are also forwarded to it.
    """

    def __init__(self, screen, switch: DisplaySwitch) -> None:
        self.screen = screen
        self.switch = switch
        self._phone: tuple[_Source, InputSink] | None = None

    def attach_phone(self, frame: _Source, sink: InputSink) -> None:
        self._phone = (frame, sink)

    def _phone_sink(self) -> InputSink | None:
        """The phone side gets input whenever the car isn't looking at the status
        screen (phone video or the Pi's launcher)."""
        phone = self._phone
        if phone and not self.switch.showing(self.switch.canvas):
            return phone[1]
        return None

    def on_pointer(self, x: int, y: int, buttons: int) -> None:
        self.screen.on_pointer(x, y, buttons)
        sink = self._phone_sink()
        if sink:
            sink.on_pointer(x, y, buttons)

    def on_key(self, keysym: int, down: bool) -> None:
        self.screen.on_key(keysym, down)
        sink = self._phone_sink()
        if sink:
            sink.on_key(keysym, down)

    def on_client(self, peer: str, pixel_format: str) -> None:
        self.screen.on_client(peer, pixel_format)

    def on_frame(self) -> None:
        self.screen.on_frame()
