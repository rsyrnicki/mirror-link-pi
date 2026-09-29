"""MirrorLink VNC message layouts (Part 2) and the DAP stub (Part 4)."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

from mlpi import dap
from mlpi import mirrorlink_vnc as ml
from mlpi.session import Session


def test_server_display_configuration_layout():
    msg = ml.server_display_configuration(1, 1)
    assert msg[:4] == bytes([128, 1, 0, 12])
    assert struct.unpack("!BBHHHI", msg[4:]) == (1, 1, 0, 1, 1, 0x00010001)


def test_server_event_configuration_layout():
    msg = ml.server_event_configuration()
    assert msg[:4] == bytes([128, 3, 0, 28])
    decoded = ml.decode_event_configuration(msg[4:])
    assert decoded["keyboard"] == "en-US"
    assert decoded["knob_keys"] == "0x0000008b"          # shift x/y, push, rotate z
    assert int(decoded["device_keys"], 16) & (1 << 12)    # Device_Backward (Part 2 §7.4)
    assert int(decoded["key_related"], 16) & (1 << 3)     # event mapping "shall be 1"
    assert decoded["pointer_events"] and decoded["button_mask"] == 1


def test_client_display_configuration_decoding_tolerates_longer_payload():
    payload = struct.pack("!BBHHHHHHII", 1, 1, 0, 800, 480, 155, 93, 900, 0x10001, 0) + b"xx"
    d = ml.decode_client_display_configuration(payload)
    assert (d["major"], d["minor"], d["width_px"], d["height_mm"], d["distance_mm"]) == \
        (1, 1, 800, 93, 900)


def test_device_status_round_trip():
    msg = ml.device_status(driver_distraction=ml.DS_ENABLED)
    assert msg[:4] == bytes([128, 11, 0, 4])
    d = ml.decode_device_status(struct.unpack("!I", msg[4:])[0])
    assert d["driver_distraction"] == ml.DS_ENABLED
    assert d["device_lock"] == ml.DS_DISABLED
    assert (d["rotation"], d["orientation"]) == (0b100, 0b10)


def test_context_information_rect_layout():
    rect = ml.context_information_rect(800, 480, app_id=2, trust=0x80, app_category=0x10001)
    x, y, w, h, enc = struct.unpack_from("!HHHHi", rect)
    assert (x, y, w, h, enc) == (0, 0, 800, 480, -524)
    assert struct.unpack_from("!IHHIII", rect, 12) == (2, 0x80, 0x80, 0x10001, 0, 0)
    assert len(rect) == 12 + 20


def test_touch_event_decoding():
    payload = bytes([2]) + struct.pack("!HHBB", 10, 20, 0, 255) + struct.pack("!HHBB", 30, 40, 1, 0)
    assert ml.decode_touch_event(payload) == [
        {"x": 10, "y": 20, "id": 0, "pressure": 255}, {"x": 30, "y": 40, "id": 1, "pressure": 0}]


def test_dap_stub_answers_not_available(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    session = Session(tmp_path / "0001")
    server = dap.DapServer(bind_address="127.0.0.1", port=port, server_version="1.1",
                           session=session)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)
    request = (b"<attestationRequest><version><majorVersion>1</majorVersion>"
               b"<minorVersion>0</minorVersion></version><trustRoot>abc=</trustRoot>"
               b"<nonce>xyz=</nonce><componentID>*</componentID></attestationRequest>")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request[:40])
        sock.sendall(request[40:])          # split across TCP segments
        reply = sock.recv(4096).decode()
    server.stop()
    time.sleep(0.2)
    session.close()
    assert "<result>1</result>" in reply
    assert "<minorVersion>0</minorVersion>" in reply   # never above the client's version
    events = [json.loads(line) for line in (tmp_path / "0001/events.jsonl").open()]
    req = next(e for e in events if e["kind"] == "dap_request")
    assert req["component"] == "*" and req["trust_root"] == "abc=" and req["minor"] == "0"
