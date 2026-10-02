from __future__ import annotations

import struct
import types

from mlpi import phone as ph


def test_touch_message_matches_scrcpy_test_vector():
    # app/tests/test_control_msg_serialize.c: test_serialize_inject_touch_event
    msg = ph.touch_message(ph.ACTION_DOWN, 100, 200, 1080, 1920, pointer_id=0x1234567887654321,
                           pressure=1.0, action_button=1, buttons=1)
    assert msg == bytes([
        2, 0x00,
        0x12, 0x34, 0x56, 0x78, 0x87, 0x65, 0x43, 0x21,
        0x00, 0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0xc8,
        0x04, 0x38, 0x07, 0x80,
        0xff, 0xff,
        0x00, 0x00, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x01])


def test_keycode_back_and_start_app_match_scrcpy_test_vectors():
    assert ph.keycode_message(1, 0x42, repeat=5, metastate=0x41) == bytes(
        [0, 1, 0, 0, 0, 0x42, 0, 0, 0, 5, 0, 0, 0, 0x41])
    assert ph.back_or_screen_on_message(1) == bytes([4, 1])
    assert ph.start_app_message("firefox") == bytes([16, 7]) + b"firefox"


def test_generic_finger_pointer_id():
    msg = ph.touch_message(ph.ACTION_UP, 1, 2, 800, 480, pressure=0.0)
    assert msg[2:10] == b"\xff" * 7 + b"\xfe"
    assert msg[22:24] == b"\x00\x00"


def _media(payload: bytes, *, config=False, key=False, pts=7) -> bytes:
    head = pts | (1 << 62 if config else 0) | (1 << 61 if key else 0)
    return struct.pack("!QI", head, len(payload)) + payload


def test_stream_parser_handles_all_packet_kinds_in_arbitrary_chunks():
    stream = (b"\x00" + b"Galaxy A56".ljust(64, b"\0") + struct.pack("!I", ph.CODEC_H264)
              + struct.pack("!III", 0x80000000, 800, 480)
              + _media(b"SPSPPS", config=True, pts=0)
              + _media(b"frame1", key=True, pts=1000)
              + struct.pack("!III", 0x80000001, 480, 800)
              + _media(b"frame2", pts=2000))
    parser = ph.StreamParser()
    events = []
    for i in range(0, len(stream), 5):            # dribble the bytes in
        events += parser.feed(stream[i:i + 5])
    assert events == [
        ("dummy",), ("device", "Galaxy A56"), ("codec", ph.CODEC_H264),
        ("session", 800, 480, False),
        ("packet", True, False, 0, b"SPSPPS"),
        ("packet", False, True, 1000, b"frame1"),
        ("session", 480, 800, True),
        ("packet", False, False, 2000, b"frame2")]


def _dns_name(name: str) -> bytes:
    return ph._encode_name(name)


def test_mdns_query_and_response_parsing():
    q = ph.mdns_query()
    assert q[:12] == struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    assert q.endswith(struct.pack("!HH", 12, 0x8001))

    service = "_adb-tls-connect._tcp.local"
    instance = "adb-R5CX123-abc." + service
    host = "Android-7.local"
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 2)
    ptr_rdata = _dns_name(instance)
    ptr = _dns_name(service) + struct.pack("!HHIH", 12, 1, 120, len(ptr_rdata)) + ptr_rdata
    # SRV name uses a compression pointer to the instance name inside the PTR rdata.
    pointer = struct.pack("!H", 0xC000 | (12 + len(_dns_name(service)) + 10))
    srv_rdata = struct.pack("!HHH", 0, 0, 41234) + _dns_name(host)
    srv = pointer + struct.pack("!HHIH", 33, 0x8001, 120, len(srv_rdata)) + srv_rdata
    a = _dns_name(host) + struct.pack("!HHIH", 1, 0x8001, 120, 4) + bytes([192, 168, 8, 44])
    parsed = ph.parse_mdns(header + ptr + srv + a)
    assert parsed["ptr"] == [instance]
    assert parsed["srv"] == {instance: (host, 41234)}
    assert parsed["a"] == {host: "192.168.8.44"}


def test_hotspot_commands_create_then_modify():
    s = ph.HotspotSettings("wlan0", "192.168.8.1/24", "MirrorLink-Pi", "secret123", "DE", 6)
    create = ph.hotspot_commands(s, exists=False)
    assert create[0][:4] == ["nmcli", "connection", "add", "type"]
    assert create[-1] == ["nmcli", "connection", "up", "mlpi-hotspot"]
    modify = ph.hotspot_commands(s, exists=True)
    flat = modify[0]
    assert flat[:4] == ["nmcli", "connection", "modify", "mlpi-hotspot"]
    settings = dict(zip(flat[4::2], flat[5::2], strict=True))
    assert settings["802-11-wireless.mode"] == "ap"
    assert settings["ipv4.addresses"] == "192.168.8.1/24"
    assert settings["802-11-wireless-security.psk"] == "secret123"
    assert settings["802-11-wireless.powersave"] == "2"          # off: avoids stalls


def test_ensure_hotspot_refuses_short_password_and_runs_nmcli():
    s = ph.HotspotSettings("wlan0", "192.168.8.1/24", "X", "short", "DE", 6)
    assert "no wifi_password" in ph.ensure_hotspot(s, run=None)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="Wired\n", stderr="")
    s.password = "long-enough-pass"
    assert "up on wlan0" in ph.ensure_hotspot(s, run=run)
    assert calls[0][:3] == ["raspi-config", "nonint", "do_wifi_country"]
    assert ["nmcli", "connection", "up", "mlpi-hotspot"] in calls
    assert ["iw", "dev", "wlan0", "set", "power_save", "off"] in calls

    def run_without_iw(cmd, **kw):
        if cmd[0] == "iw":
            raise FileNotFoundError("iw")
        return run(cmd, **kw)
    assert "up on wlan0" in ph.ensure_hotspot(s, run=run_without_iw)


def test_pointer_mapping_scales_to_video_and_sends_down_move_up():
    sent = []
    frame = types.SimpleNamespace(width=800, height=480)
    cfg = types.SimpleNamespace(adb="adb", adb_home="")
    link = ph.PhoneLink(cfg, frame, switch=None)
    link._send = sent.append
    link._video_size = (1600, 960)                # phone streams at twice the size
    link.on_pointer(10, 20, 1)
    link.on_pointer(10, 20, 1)                    # no movement: nothing sent
    link.on_pointer(30, 40, 1)
    link.on_pointer(30, 40, 0)
    actions = [(m[1], struct.unpack_from("!ii", m, 10)) for m in sent]
    assert actions == [(0, (20, 40)), (2, (60, 80)), (1, (60, 80))]
    assert all(struct.unpack_from("!HH", m, 18) == (1600, 960) for m in sent)
    link.on_key(0x3000020C, True)                 # MirrorLink Device_Backward
    assert sent[-1] == ph.keycode_message(0, ph.KEYCODE_BACK)


def test_missing_adb_binary_is_a_status_not_a_crash():
    adb = ph.Adb("/nonexistent/adb")
    assert adb.run("devices").returncode == 127
    frame = types.SimpleNamespace(width=800, height=480)
    cfg = types.SimpleNamespace(adb="adb", adb_home="", legacy_port=5555, interface="")
    link = ph.PhoneLink(cfg, frame, switch=None, serial="1.2.3.4:5555", adb=adb)
    assert link._find_device() == ""
    assert "adb unavailable" in link.status


def test_display_power_matches_scrcpy_test_vector():
    # test_serialize_set_display_power: {SC_CONTROL_MSG_TYPE_SET_DISPLAY_POWER, 1}
    assert ph.display_power_message(True) == bytes([10, 1])
    assert ph.display_power_message(False) == bytes([10, 0])


def test_portrait_app_is_pillarboxed_and_touches_map_into_it():
    assert ph.fit_box(800, 480, 800, 480) == (0, 0, 800, 480)
    assert ph.fit_box(480, 800, 800, 480) == (256, 0, 288, 480)

    sent = []
    frame = types.SimpleNamespace(width=800, height=480)
    link = ph.PhoneLink(types.SimpleNamespace(adb="adb", adb_home=""), frame, switch=None)
    link._send = sent.append
    link._video_size, link._video_box = (480, 800), (256, 0, 288, 480)
    link.on_pointer(100, 240, 1)                 # on the black bar: ignored
    assert sent == []
    link.on_pointer(400, 240, 1)                 # middle of the picture
    link.on_pointer(400, 240, 0)
    assert [struct.unpack_from("!ii", m, 10) for m in sent] == [(240, 400), (240, 400)]


def test_app_list_uses_its_own_jar_copy():
    calls = []

    class FakeAdb:
        def run(self, *args, serial="", timeout=20.0):
            calls.append(args)
            out = (" - Spotify                        com.spotify.music\n"
                   if args[0] == "shell" else "")
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    cfg = types.SimpleNamespace(adb="adb", adb_home="", server_jar="/opt/jar", launcher=False)
    link = ph.PhoneLink(cfg, types.SimpleNamespace(width=800, height=480), switch=None,
                        adb=FakeAdb())
    link._load_app_list("s")
    assert calls[0] == ("push", "/opt/jar", ph.REMOTE_LIST_JAR)
    assert f"CLASSPATH={ph.REMOTE_LIST_JAR}" in calls[1]


def test_lag_guard_skips_to_next_keyframe():
    from mlpi.phone import LagGuard, reset_video_message
    assert reset_video_message() == b"\x11"
    g = LagGuard(0.5)
    assert g.decode(0, True, 100.0)
    assert g.decode(100_000, False, 100.15)         # 50 ms jitter: fine
    assert not g.decode(200_000, False, 101.0)      # 0.8 s behind: skip
    assert g.skipping and g.skips == 1
    assert not g.decode(300_000, False, 101.01)     # still no keyframe
    assert g.decode(5_000_000, True, 101.02)        # keyframe: live again, new baseline
    assert g.decode(5_033_000, False, 101.06)
    assert g.skips == 1


def test_lag_guard_restart_forgets_old_timestamps():
    from mlpi.phone import LagGuard
    g = LagGuard(0.5)
    assert g.decode(50_000_000, True, 10.0)
    g.restart()                                     # rotation: pts start over at 0
    assert g.decode(0, True, 11.0)
    assert g.decode(33_000, False, 11.05) and g.skips == 0
    off = LagGuard(0)                               # max_lag 0 = never skip
    assert off.decode(0, False, 0.0) and off.decode(0, False, 60.0)


def test_avoid_bad_wifi_is_set_once():
    calls = []
    current = {"v": "null"}

    class FakeAdb:
        def run(self, *args, serial="", timeout=20.0):
            calls.append(args)
            if args[1:3] == ("settings", "put"):
                current["v"] = args[-1]
            return types.SimpleNamespace(returncode=0, stdout=current["v"] + "\n", stderr="")

    cfg = types.SimpleNamespace(adb="adb", adb_home="", server_jar="/opt/jar", launcher=False)
    link = ph.PhoneLink(cfg, types.SimpleNamespace(width=800, height=480), switch=None,
                        adb=FakeAdb())
    link._avoid_bad_wifi("s")
    assert ("shell", "settings", "put", "global", "network_avoid_bad_wifi", "1") in calls
    calls.clear()
    link._avoid_bad_wifi("s")                       # already 1: only reads
    assert len(calls) == 1 and calls[0][2] == "get"


def test_fit_box_width_is_simd_safe():
    for src in ((480, 800), (720, 1600), (1080, 2340), (600, 1024)):
        x, _y, w, _h = ph.fit_box(*src, 800, 480)
        assert w % 16 == 0 and 2 * x + w <= 800


def test_port_scan_finds_a_listener():
    import socket as sk
    srv = sk.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    try:
        assert port in ph.scan_open_ports("127.0.0.1", port - 50, port + 50)
    finally:
        srv.close()


def test_phone_found_by_scan_when_mdns_is_silent(tmp_path, monkeypatch):
    """Screen off: no mDNS answer, so the Pi scans; next time it tries that port first."""
    from mlpi.session import Session
    connects = []

    class FakeAdb:
        def devices(self):
            return []

        def connect(self, target):
            connects.append(target)
            return target.endswith(":41669")

        def state(self, target):
            return "device"

    scans = []
    monkeypatch.setattr(ph, "discover_adb_tls", lambda *a, **k: [])
    monkeypatch.setattr(ph, "scan_open_ports", lambda ip, a, b: scans.append(ip) or [41669])
    session = Session(tmp_path / "sessions" / "0001")
    cfg = types.SimpleNamespace(adb="adb", adb_home="", legacy_port=5555, interface="wlan0")
    link = ph.PhoneLink(cfg, types.SimpleNamespace(width=800, height=480), switch=None,
                        session=session, candidates=lambda: ["192.168.8.44"], adb=FakeAdb())
    assert link._find_device() == "192.168.8.44:41669"
    assert scans == ["192.168.8.44"]
    assert (tmp_path / "phone-adb-port").read_text().strip() == "41669"

    connects.clear()
    link2 = ph.PhoneLink(cfg, types.SimpleNamespace(width=800, height=480), switch=None,
                         session=session, candidates=lambda: ["192.168.8.44"], adb=FakeAdb())
    assert link2._find_device() == "192.168.8.44:41669"
    assert connects == ["192.168.8.44:5555", "192.168.8.44:41669"]   # remembered: no scan
    assert scans == ["192.168.8.44"]
    session.close()


def test_car_keyboard_types_text_and_backspace_deletes_after_typing():
    # test_serialize_inject_text: {SC_CONTROL_MSG_TYPE_INJECT_TEXT, 0, 0, 0, 13, "hello, world!"}
    assert ph.text_message("hello, world!") == bytes([1, 0, 0, 0, 13]) + b"hello, world!"
    assert ph.keysym_char(0x61) == "a" and ph.keysym_char(0xE4) == "ä"
    assert ph.keysym_char(0x010020AC) == "€" and ph.keysym_char(0xFFB7) == "7"
    assert ph.keysym_char(0xFF08) == "" and ph.keysym_char(0x30000000) == ""

    sent = []
    link = ph.PhoneLink(types.SimpleNamespace(adb="adb", adb_home=""),
                        types.SimpleNamespace(width=800, height=480), switch=None)
    link._send = sent.append
    link.on_key(0xFF08, True)                     # nothing typed yet: BackSpace = Back
    assert sent[-1] == ph.keycode_message(0, ph.KEYCODE_BACK)
    link.on_key(0x61, True)
    link.on_key(0x61, False)                      # the release types nothing more
    assert sent[-1] == ph.text_message("a")
    link.on_key(0xFF08, True)
    assert sent[-1] == ph.keycode_message(0, ph.KEYCODE_DEL)
    link.on_key(0xFF0D, False)
    assert sent[-1] == ph.keycode_message(1, ph.KEYCODE_ENTER)


CONNECTIVITY_WIFI_DEFAULT = """\
Active default network: 175
  NetworkAgentInfo{network{172}  ni{MOBILE[NR] CONNECTED extra: apn} Score(...IS_VALIDATED)
  NetworkAgentInfo{network{175}  ni{WIFI CONNECTED extra: } Score(...ACCEPT_UNVALIDATED)
"""


def test_wifi_as_default_network_is_detected():
    assert ph.wifi_is_default_network(CONNECTIVITY_WIFI_DEFAULT)
    assert not ph.wifi_is_default_network(
        CONNECTIVITY_WIFI_DEFAULT.replace("network: 175", "network: 172"))
    assert not ph.wifi_is_default_network("")


def test_stuck_offline_connection_is_dropped_and_adb_restarted():
    calls = []

    class FakeAdb:
        def connect(self, target):
            return True

        def state(self, target):
            return "offline"

        def run(self, *args, serial="", timeout=20.0):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    link = ph.PhoneLink(types.SimpleNamespace(adb="adb", adb_home=""),
                        types.SimpleNamespace(width=800, height=480), switch=None,
                        adb=FakeAdb())
    for _ in range(ph.OFFLINE_RESTART_AFTER):
        assert link._try_targets(["192.168.8.45:40445"]) == ""
    assert calls.count(("disconnect", "192.168.8.45:40445")) == ph.OFFLINE_RESTART_AFTER
    assert calls.count(("kill-server",)) == 1


def test_only_addresses_of_connected_phones_are_tried():
    from mlpi.dhcp import DhcpServer
    d = DhcpServer(interface="wlan0", server_ip="192.168.8.1", prefix=24,
                   client_ip="192.168.8.44", offer_router=False, offer_dns=False,
                   session=None, is_car=False)
    assert d.lease_for("aa:aa:aa:aa:aa:aa") == "192.168.8.44"      # before "Forget"
    assert d.lease_for("bb:bb:bb:bb:bb:bb") == "192.168.8.45"      # new random MAC
    assert d.connected_addresses(["BB:BB:BB:BB:BB:BB"]) == ["192.168.8.45"]
    assert d.connected_addresses([]) == []
    assert d.connected_addresses(None) == ["192.168.8.45", "192.168.8.44"]
