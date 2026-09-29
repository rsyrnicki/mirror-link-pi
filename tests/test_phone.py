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
