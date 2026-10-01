"""car-view against a local VNC server fed with changing 'phone video'."""

from __future__ import annotations

import socket
import threading
import time

from mlpi.canvas import Canvas
from mlpi.rfb import RfbServer
from mlpi.tools.car_view import CarClient
from mlpi.video import DisplaySwitch


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Screen:
    def __init__(self) -> None:
        self.pointers: list[tuple[int, int, int]] = []

    def on_pointer(self, x, y, buttons): self.pointers.append((x, y, buttons))
    def on_key(self, keysym, down): pass
    def on_client(self, peer, pixel_format): pass
    def on_frame(self): pass


def test_car_client_streams_updates_and_sends_touches():
    w, h = 160, 96
    switch = DisplaySwitch(Canvas(w, h))
    video = switch.new_video_frame()
    switch.show(video)
    screen = _Screen()
    port = _free_port()
    rfb = RfbServer(bind_address="127.0.0.1", port=port, canvas=switch, name="test",
                    screen=screen, context_info=lambda: (2, 0x80, 0x10001, 0))
    threading.Thread(target=rfb.serve_forever, daemon=True).start()
    stop = threading.Event()

    def phone() -> None:            # a new frame every 20 ms
        n = 0
        while not stop.is_set():
            n += 1
            video.update(bytes([n & 0xFF, 0]) * (w * h))
            stop.wait(0.02)
    threading.Thread(target=phone, daemon=True).start()
    time.sleep(0.2)
    client = CarClient("127.0.0.1", port, verbose=False)
    threading.Thread(target=client.run, daemon=True).start()
    try:
        time.sleep(0.5)
        client.pointer(1, 50, 40)
        client.pointer(0, 50, 40)
        time.sleep(0.8)
        assert client.updates >= 10, client.error
        assert client.tap_ms and client.tap_ms[0] < 500
        assert (50, 40, 1) in screen.pointers and (50, 40, 0) in screen.pointers
        rgb = client.rgb()
        assert len(rgb) == w * h * 3
    finally:
        stop.set()
        client.stop()
        rfb.stop()
