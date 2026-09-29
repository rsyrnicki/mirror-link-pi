from __future__ import annotations

import struct

from mlpi import usbhost


def test_ml_version_encoding():
    assert usbhost.ML_VERSIONS["1.1"] == 0x0101
    assert usbhost.ML_VERSIONS["1.3"] == 0x0301


def test_control_struct_is_24_bytes_pointer_at_16():
    packed = struct.pack(usbhost._CTRL_FMT, 0x40, 0xF0, 0x0101, 0x1D6B, 0, 1000, 0x1234)
    assert len(packed) == 24
    assert struct.unpack_from("<Q", packed, 16)[0] == 0x1234
    assert struct.unpack_from("<I", packed, 8)[0] == 1000     # timeout


def test_ioctl_number_matches_iowr():
    size = struct.calcsize(usbhost._CTRL_FMT)
    expected = (3 << 30) | (size << 16) | (ord("U") << 8) | 0
    assert usbhost._USBDEVFS_CONTROL == expected


def test_known_vendors_cover_samsung():
    assert usbhost.KNOWN_VENDORS[0x04E8] == "Samsung"
