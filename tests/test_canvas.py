from __future__ import annotations

from mlpi import canvas as cv


def test_all_glyphs_are_5x7():
    for ch, runs in cv.GLYPHS.items():
        for y, x, length in runs:
            assert 0 <= y < cv.GLYPH_H and 0 <= x and x + length <= cv.GLYPH_W, ch


def test_encoder_rgb565_little_endian():
    pf = cv.PixelFormat(16, 16, False, True, 31, 63, 31, 11, 5, 0)
    enc = cv.PixelEncoder(pf)
    out = enc.encode_row(bytes([cv.RED, cv.GREEN, cv.BLUE, cv.WHITE]))
    # WHITE is (245,245,245) → R30 G61 B30 → 0xF7BE
    assert out == bytes.fromhex("00f8" "e007" "1f00" "bef7")


def test_encoder_32bpp_big_endian():
    pf = cv.PixelFormat(32, 24, True, True, 255, 255, 255, 16, 8, 0)
    enc = cv.PixelEncoder(pf)
    assert enc.encode_row(bytes([cv.RED, cv.BLUE])) == bytes.fromhex("00ff0000" "000000ff")


def test_encoder_colour_map_uses_palette_index():
    pf = cv.PixelFormat(8, 8, False, False, 0, 0, 0, 0, 0, 0)
    enc = cv.PixelEncoder(pf)
    assert enc.encode_row(bytes([3, 7])) == bytes([3, 7])
    msg = cv.colour_map_entries()
    assert msg[0] == 1 and len(msg) == 6 + 6 * len(cv.PALETTE)


def test_pixel_format_round_trip():
    pf = cv.DEFAULT_PIXEL_FORMAT
    assert cv.PixelFormat.unpack(pf.pack()) == pf
    assert len(pf.pack()) == 16


def test_dirty_tracking():
    c = cv.Canvas(100, 50)
    v0 = c.version
    c.fill_rect(10, 10, 5, 5, cv.RED)
    c.fill_rect(40, 20, 2, 2, cv.RED)
    c.commit()
    version, box = c.changes_since(v0)
    assert version == v0 + 1
    assert box == (10, 10, 42, 22)
    assert c.changes_since(version) == (version, None)
    assert c.changes_since(-1)[1] == (0, 0, 100, 50)


def test_text_draws_pixels_and_clips():
    c = cv.Canvas(40, 10)
    c.text(1, 1, "Hi", scale=1, colour=cv.WHITE)
    c.text(35, 1, "WWWW", scale=1)   # runs off the edge: must not raise
    assert cv.WHITE in c.pixels
