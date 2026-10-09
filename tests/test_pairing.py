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


def test_pairing_page_appears_at_once_when_debugging_is_on_but_key_refused(monkeypatch):
    class FakeAdb:
        def devices(self):
            return []

        def connect(self, target):
            return False                     # TLS: the phone doesn't know our key

    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", interface="wlan0", launcher=False,
                                legacy_port=0)
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch, adb=FakeAdb(),
                        candidates=lambda: ["192.168.8.44"])
    monkeypatch.setattr(ph, "discover_adb_tls",
                        lambda *a, service=ph.ADB_TLS_SERVICE, **k:
                        [("192.168.8.44", 41749)] if service == ph.ADB_TLS_SERVICE else [])
    monkeypatch.setattr(ph, "scan_open_ports", lambda *a, **k: [])
    assert link._find_device() == ""
    assert switch.showing(link.pairing.frame)                 # no 20 s wait


def test_car_keyboard_types_the_pairing_code():
    paired = []

    class FakeAdb:
        def pair(self, target, code):
            paired.append((target, code))
            return False, "Failed"

    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", interface="wlan0", launcher=False)
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch, adb=FakeAdb())
    link._show_pairing("192.168.8.44")
    link.pairing.set_port(37001)
    for keysym in (0x31, 0x32, 0x33, 0x34, 0x35, 0x39, 0xFF08, 0x36):
        link.on_key(keysym, True)
        link.on_key(keysym, False)
    assert link.pairing.code == "123456"
    link.on_key(0xFF0D, False)                                 # Return = PAIR
    for _ in range(100):
        if paired:
            break
        time.sleep(0.05)
    assert paired == [("192.168.8.44:37001", "123456")]


def test_connecting_without_pairing_says_so_before_leaving_the_page(monkeypatch):
    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", interface="wlan0", launcher=False)
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch)
    link._show_pairing("192.168.8.44")
    link.pairing.press(("digit", "4"))
    monkeypatch.setattr(link._stop, "wait", lambda t: False)
    link._leave_pairing()
    assert "NO PAIRING NEEDED" not in link.pairing.message     # reset afterwards
    assert link.pairing.code == ""


def test_hints_never_replace_a_pairing_result():
    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", interface="wlan0", launcher=False)
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch)
    link._show_pairing("192.168.8.44", ph.NOT_FOUND_HINT)
    assert link.pairing.message == ph.NOT_FOUND_HINT
    link._show_pairing("192.168.8.44", ph.REFUSED_HINT)
    assert link.pairing.message == ph.REFUSED_HINT
    link.pairing.set_message("PAIRING FAILED: CHECK CODE AND PORT")
    link._show_pairing("192.168.8.44", ph.NOT_FOUND_HINT)
    assert link.pairing.message.startswith("PAIRING FAILED")


def test_debugging_port_refused_before_pairing_is_tried_again_after_it(monkeypatch):
    """Before pairing the phone's real debugging port refuses the Pi (and so lands on
    the skip list); after pairing it must be tried again at once."""
    accepted = set()

    class FakeAdb:
        def devices(self):
            return []

        def connect(self, target):
            return target in accepted

        def state(self, target):
            return "device"

        def pair(self, target, code):
            accepted.add("192.168.8.44:38959")      # the phone now knows this Pi's key
            return True, "Successfully paired"

        def run(self, *args, serial="", timeout=20.0):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ph, "discover_adb_tls", lambda *a, **k: [])
    monkeypatch.setattr(ph, "scan_open_ports", lambda ip, a, b: [38959])
    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", interface="wlan0", launcher=False,
                                legacy_port=0)
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch, adb=FakeAdb(),
                        candidates=lambda: ["192.168.8.44"])
    monkeypatch.setattr(link._stop, "wait", lambda t: False)
    for _ in range(3):                                # refused: skipped after two tries
        link._last_scan = float("-inf")
        assert link._find_device() == ""
    link._pair("192.168.8.44", "37001", "123456")
    assert link._find_device() == "192.168.8.44:38959"
