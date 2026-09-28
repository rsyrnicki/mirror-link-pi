"""Full round trip on localhost: recorded car handshake → LaunchApplication → VNC frame."""

from __future__ import annotations

import json
import socket
import threading

from mlpi.canvas import Canvas
from mlpi.config import Config
from mlpi.http_descriptor import DescriptorServer
from mlpi.rfb import RfbServer
from mlpi.runner import context_info
from mlpi.screen import StatusScreen
from mlpi.session import STAGE_VNC_FRAMES, Session
from mlpi.tools import report, simulate_car
from mlpi.variants import Variant, VariantManager


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_simulated_car_gets_a_vnc_frame(tmp_path):
    cfg = Config()
    cfg.network.http_port = _free_port()
    cfg.network.vnc_port = _free_port()
    session = Session(tmp_path / "0001")
    variants = VariantManager([Variant(name="spec-1.0"), Variant(name="ml11", ml_version="1.1")],
                              session=session, attempt_gap_seconds=4.0, state_dir=tmp_path)
    canvas = Canvas(320, 200)
    screen = StatusScreen(canvas, session=session, variant_name=lambda: variants.current.name)
    http = DescriptorServer(cfg, "127.0.0.1", session=session, variants=variants)
    rfb = RfbServer(bind_address="127.0.0.1", port=cfg.network.vnc_port, canvas=canvas,
                    name="test", session=session, on_connect=lambda p: variants.vnc_connected(),
                    screen=screen, dump_dir=session.directory,
                    context_info=lambda: context_info(variants.current))
    threads = [threading.Thread(target=f, daemon=True)
               for f in (http.serve_forever, rfb.serve_forever, screen.run)]
    for t in threads:
        t.start()
    try:
        shot = tmp_path / "car-view.png"
        rc = simulate_car.run(target="127.0.0.1", http_port=cfg.network.http_port,
                              screenshot=shot, attempts=2)
        assert rc == 0
        assert shot.read_bytes().startswith(b"\x89PNG")
    finally:
        http.shutdown()
        rfb.stop()
        screen.stop()
        session.close()

    events = [json.loads(line) for line in (tmp_path / "0001/events.jsonl").open()]
    kinds = {e["kind"] for e in events}
    assert {"http_request", "http_response", "notify", "vnc_connect",
            "vnc_first_update", "vnc_pointer"} <= kinds
    assert session.stage == STAGE_VNC_FRAMES
    starts = [e for e in events if e["kind"] == "attempt_start"]
    assert [e["variant"] for e in starts] == ["spec-1.0", "ml11"]
    assert all(e["simulated"] for e in starts)
    assert not variants.locked       # simulator traffic never locks a variant
    assert (tmp_path / "0001/vnc-1-rx.bin").read_bytes().startswith(b"RFB 003.008\n")
    ml_names = [e["name"] for e in events if e["kind"] == "vnc_mirrorlink_msg"]
    assert ml_names == ["ClientDisplayConfiguration", "ClientEventConfiguration",
                        "DeviceStatusRequest", "ByeBye"]
    cdc = next(e for e in events if e.get("name") == "ClientDisplayConfiguration")
    assert cdc["decoded"]["width_px"] == 320 and cdc["decoded"]["width_mm"] == 155
    assert any(e["kind"] == "vnc_context_info_sent" for e in events)

    text = report.summarise(tmp_path / "0001")
    assert "FURTHEST STAGE: 6" in text
    assert "LaunchApplication" in text
