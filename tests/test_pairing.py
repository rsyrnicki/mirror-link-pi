"""Pairing the phone from the car screen."""

from __future__ import annotations

import struct
import time
import types

import mlpi.phone as ph
from mlpi.canvas import Canvas
from mlpi.pairing import PairingPage
from mlpi.video import DisplaySwitch, VideoFrame


def _center(box):
    x, y, w, h = box
    return x + w // 2, y + h // 2


def _box(page, target):
    return next(b for b, t in page.targets if t == target)


def test_page_collects_code_and_port():
    page = PairingPage(VideoFrame(800, 480))
    assert page.press(("pair", None)) is None and "6 DIGITS" in page.message
    for d in "123456":
        page.press(("digit", d))
    assert page.code == "123456" and page.field == "port"     # no port yet: port next
    for d in "41749":
        page.press(("digit", d))
    assert page.port == "41749" and page.ready()
    assert page.press(("pair", None)) == "pair"
    page.set_port(37001)                                       # announced: replaces it
    assert page.port == "37001" and page.port_found and page.field == "code"
    page.press(("del", None))
    assert page.code == "12345" and not page.ready()


def _mdns_srv_answer(instance: str, port: int, host: str, ip: str) -> bytes:
    def name(n):
        return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"
    srv = struct.pack("!HHH", 0, 0, port) + name(host)
    a = bytes(int(x) for x in ip.split("."))
    rr = (name(instance) + struct.pack("!HHIH", ph.TYPE_SRV, 1, 120, len(srv)) + srv
          + name(host) + struct.pack("!HHIH", ph.TYPE_A, 1, 120, 4) + a)
    return struct.pack("!HHHHHH", 0, 0x8400, 0, 2, 0, 0) + rr


def test_pairing_answer_is_told_apart_from_connect_answer():
    data = _mdns_srv_answer("adb-R5CX-abc._adb-tls-pairing._tcp.local", 37001,
                            "Android.local", "192.168.8.44")
    srv = ph.parse_mdns(data)["srv"]
    (instance, (_host, port)), = srv.items()
    assert instance.endswith(ph.ADB_PAIRING_SERVICE) and port == 37001
    assert not instance.endswith(ph.ADB_TLS_SERVICE)


def test_car_screen_pairing_runs_adb_pair(monkeypatch):
    paired = []

    class FakeAdb:
        def pair(self, target, code):
            paired.append((target, code))
            return True, "Successfully paired to 192.168.8.44:37001 [guid=adb-x]"

    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", interface="wlan0", launcher=False)
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch, adb=FakeAdb())
    monkeypatch.setattr(ph, "discover_adb_tls",
                        lambda *a, service=ph.ADB_TLS_SERVICE, **k:
                        [("192.168.8.44", 37001)] if service == ph.ADB_PAIRING_SERVICE else [])
    assert link._offer_pairing("192.168.8.44")
    assert switch.showing(link.pairing.frame) and link.pairing.port == "37001"

    def tap(target):
        x, y = _center(_box(link.pairing, target))
        link.on_pointer(x, y, 1)
        link.on_pointer(x, y, 0)

    for d in "482193":
        tap(("digit", d))
    tap(("pair", None))
    for _ in range(100):
        if paired and not switch.showing(link.pairing.frame):
            break
        time.sleep(0.05)
    assert paired == [("192.168.8.44:37001", "482193")]
    assert switch.showing(switch.canvas)                     # back to the status screen


def test_failed_pairing_keeps_the_page_with_a_message():
    class FakeAdb:
        def pair(self, target, code):
            return False, "Failed: Wrong password or connection was dropped."

    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", interface="wlan0", launcher=False)
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch, adb=FakeAdb())
    link._show_pairing("192.168.8.44")
    link._pair("192.168.8.44", "37001", "000000")
    assert switch.showing(link.pairing.frame)
    assert "FAILED" in link.pairing.message and link.pairing.code == ""
