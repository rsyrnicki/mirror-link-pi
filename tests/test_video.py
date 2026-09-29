from __future__ import annotations

import random
import struct

from mlpi.canvas import Canvas, PixelEncoder, PixelFormat
from mlpi.video import DisplaySwitch, InputRouter, Rgb565Converter, VideoFrame, is_rgb565le

RGB565LE = PixelFormat(16, 16, False, True, 31, 63, 31, 11, 5, 0)
XRGB32 = PixelFormat(32, 24, False, True, 255, 255, 255, 16, 8, 0)
BGR233 = PixelFormat(8, 8, False, True, 7, 7, 3, 0, 3, 6)


def _reference(pixel: int, pf: PixelFormat) -> int:
    r, g, b = pixel >> 11, (pixel >> 5) & 0x3F, pixel & 0x1F

    def scale(v, bits, out_max):
        s = out_max.bit_length() - bits
        return v << s if s >= 0 else v >> -s
    return (scale(r, 5, pf.red_max) << pf.red_shift | scale(g, 6, pf.green_max) << pf.green_shift
            | scale(b, 5, pf.blue_max) << pf.blue_shift)


def test_converter_matches_per_pixel_reference_for_several_formats():
    rng = random.Random(1)
    pixels = [rng.randrange(65536) for _ in range(500)] + [0, 0xFFFF, 0xF800, 0x07E0, 0x001F]
    raw = struct.pack(f"<{len(pixels)}H", *pixels)
    for pf in (RGB565LE, XRGB32, BGR233, PixelFormat(16, 16, True, True, 31, 63, 31, 11, 5, 0)):
        out = Rgb565Converter(pf).convert(raw)
        bpp = pf.bits_per_pixel // 8
        order = "big" if pf.big_endian else "little"
        want = b"".join(_reference(p, pf).to_bytes(bpp, order) for p in pixels)
        assert out == want, pf.describe()


def test_rgb565le_is_a_plain_copy():
    assert is_rgb565le(RGB565LE) and not is_rgb565le(XRGB32)
    raw = bytes(range(200))
    assert Rgb565Converter(RGB565LE).convert(raw) is raw


def test_video_frame_tracks_changed_rows():
    vf = VideoFrame(4, 6)
    stride = 8
    frame = bytearray(4 * 6 * 2)
    assert not vf.update(bytes(frame))          # identical: no new version
    frame[2 * stride + 3] = 0xAA                 # row 2
    frame[4 * stride] = 0x55                     # row 4
    assert vf.update(bytes(frame))
    assert vf.changes_since(0) == (1, (0, 2, 4, 5))
    assert vf.changes_since(1) == (1, None)
    enc = PixelEncoder(RGB565LE)
    assert vf.encode_rect(enc, 0, 2, 4, 1) == bytes(frame[2 * stride:3 * stride])
    assert vf.encode_rect(enc, 1, 4, 2, 1) == bytes(frame[4 * stride + 2:4 * stride + 6])


def test_switch_forces_full_frame_on_source_change_and_router_follows():
    canvas = Canvas(8, 4)
    switch = DisplaySwitch(canvas)
    video = switch.new_video_frame()
    v0, _ = switch.changes_since(-1)
    assert switch.changes_since(v0) == (v0, None)
    switch.show(video)
    v1, box = switch.changes_since(v0)
    assert box == (0, 0, 8, 4) and v1 != v0
    video.update(b"\x01" * (8 * 4 * 2))
    v2, box = switch.changes_since(v1)
    assert box == (0, 0, 8, 4) and v2 > v1

    seen = []

    class Screen:
        def on_pointer(self, x, y, b): seen.append(("screen", x, y, b))
        def on_key(self, k, d): seen.append(("screen-key", k, d))
        def on_client(self, *a): pass
        def on_frame(self): pass

    class Phone:
        def on_pointer(self, x, y, b): seen.append(("phone", x, y, b))
        def on_key(self, k, d): seen.append(("phone-key", k, d))

    router = InputRouter(Screen(), switch)
    router.attach_phone(video, Phone())
    router.on_pointer(1, 2, 1)
    switch.show(canvas)
    router.on_pointer(3, 4, 0)
    assert seen == [("screen", 1, 2, 1), ("phone", 1, 2, 1), ("screen", 3, 4, 0)]


def test_png_snapshot():
    vf = VideoFrame(2, 1)
    vf.update(struct.pack("<2H", 0xF800, 0x001F))
    assert vf.to_png().startswith(b"\x89PNG")
