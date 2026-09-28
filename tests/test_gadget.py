"""FunctionFS blobs and MirrorLink USB command handling (no USB hardware needed)."""

from __future__ import annotations

import struct

from mlpi import gadget


def _walk_descriptors(blob: bytes) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(blob):
        length, dtype = blob[i], blob[i + 1]
        assert length >= 2
        out.append((length, dtype))
        i += length
    assert i == len(blob)
    return out


def test_ffs_descriptor_blob_matches_functionfs_v2_layout():
    blob = gadget.ffs_descriptors()
    magic, length, flags = struct.unpack_from("<III", blob)
    assert magic == 3 and length == len(blob)
    assert flags & gadget.FUNCTIONFS_ALL_CTRL_RECIP
    assert flags & gadget.FUNCTIONFS_CONFIG0_SETUP
    fs_count, hs_count = struct.unpack_from("<II", blob, 12)
    rest = blob[20:]
    fs = _walk_descriptors(rest[:9 + 7 + 7])
    hs = _walk_descriptors(rest[9 + 7 + 7:])
    assert len(fs) == fs_count == 3 and len(hs) == hs_count == 3
    assert [d[1] for d in fs] == [4, 5, 5]            # interface, endpoint, endpoint
    iface = rest[:9]
    assert iface[4] == 2 and iface[5] == 0xFF          # 2 endpoints, vendor-specific class
    hs_ep = rest[23 + 9:23 + 16]
    assert struct.unpack_from("<H", hs_ep, 4)[0] == 512


def test_ffs_strings_blob():
    blob = gadget.ffs_strings("MirrorLink")
    magic, length, str_count, lang_count = struct.unpack_from("<IIII", blob)
    assert (magic, length, str_count, lang_count) == (2, len(blob), 1, 1)
    assert blob[16:18] == b"\x09\x04" and blob.endswith(b"MirrorLink\0")


def _event(setup: tuple | None, etype: int) -> bytes:
    raw = struct.pack("<BBHHH", *setup) if setup else b"\0" * 8
    return raw + bytes([etype, 0, 0, 0])


def test_parse_ffs_events_and_ml_command():
    data = _event(None, 0) + _event((0x40, 0xF0, 0x0101, 0x1234, 0), 4) + _event(None, 2)
    events = gadget.parse_ffs_events(data)
    assert [e[0] for e in events] == ["BIND", "SETUP", "ENABLE"]
    setup = events[1][1]
    assert gadget.is_ml_command(setup)
    assert gadget.ml_version_from_wvalue(setup["wValue"]) == "1.1"
    assert gadget.ml_version_from_wvalue(0x0301) == "1.3"
    assert gadget.ml_version_from_wvalue(0x0100) == "1.0"   # "version 0.1" → 1.0 (§4.2.2)


def test_answer_setup_acks_ml_command_and_stalls_others(monkeypatch):
    calls = []
    monkeypatch.setattr(gadget.os, "read", lambda fd, n: calls.append(("read", n)) or b"")
    monkeypatch.setattr(gadget.os, "write", lambda fd, b: calls.append(("write", len(b))) or 0)
    ml = {"bmRequestType": 0x40, "bRequest": 0xF0, "wValue": 0x0101, "wIndex": 0, "wLength": 0}
    assert gadget._answer_setup(3, ml) is True
    other_out = dict(ml, bRequest=0x01)
    assert gadget._answer_setup(3, other_out) is False
    other_in = dict(ml, bmRequestType=0xC0, wLength=4)
    assert gadget._answer_setup(3, other_in) is False
    # ACK = read in the request's direction; STALL = operation in the wrong direction.
    assert calls == [("read", 0), ("write", 0), ("read", 0)]


def test_teardown_never_raises_on_a_stuck_function(tmp_path):
    # A leftover gadget whose ffs function dir can't be rmdir'd (non-empty) must not
    # abort teardown — otherwise the service crash-loops. Simulate on a normal fs.
    g = tmp_path / "g_mlpi"
    (g / "configs/c.1/strings/0x409").mkdir(parents=True)
    (g / "strings/0x409").mkdir(parents=True)
    stuck = g / "functions/ffs.mlcmd"
    stuck.mkdir(parents=True)
    (stuck / "ep0").write_text("busy")   # makes rmdir(stuck) fail with ENOTEMPTY
    (g / "functions/ncm.usb0").mkdir()
    gadget.teardown(g)                    # must not raise
    assert not (g / "functions/ncm.usb0").exists()   # the removable parts are gone
    assert not (g / "strings/0x409").exists()


def test_default_config_keeps_ml_command_off():
    from mlpi.config import Config
    assert Config().usb.ml_command is False
