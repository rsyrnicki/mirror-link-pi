"""Phone mode end to end, without a phone: a fake scrcpy 4.1 server streams real H.264
(ffmpeg's test pattern) → PhoneLink → libavcodec (ctypes) → DisplaySwitch → VNC server →
the car simulator; the simulator's touch must come back to the "phone"."""

from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import threading
import time

import pytest

from mlpi import phone as ph
from mlpi.canvas import Canvas
from mlpi.config import Config
from mlpi.rfb import RfbServer
from mlpi.screen import StatusScreen
from mlpi.session import Session
from mlpi.tools import simulate_car
from mlpi.video import DisplaySwitch, InputRouter

PHONE_W, PHONE_H = 640, 400          # the phone streams at twice the car size


def _have_x264() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return "libx264" in out.stdout


def _have_libavcodec() -> bool:
    try:
        from mlpi.avdecode import AvDecoder
        AvDecoder(16, 16).close()
    except Exception:  # noqa: BLE001
        return False
    return True


pytestmark = pytest.mark.skipif(not (_have_x264() and _have_libavcodec()),
                                reason="needs ffmpeg with libx264 and libavcodec/libswscale")


def _test_stream(tmp_path) -> list[bytes]:
    out = tmp_path / "test.h264"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         f"testsrc=size={PHONE_W}x{PHONE_H}:rate=15", "-t", "2", "-c:v", "libx264",
         "-preset", "ultrafast", "-tune", "zerolatency", "-x264-params", "aud=1:keyint=15",
         "-pix_fmt", "yuv420p", "-f", "h264", str(out)], check=True)
    data = out.read_bytes()
    marker = b"\x00\x00\x00\x01\x09"
    starts = [i for i in range(len(data)) if data.startswith(marker, i)]
    return [data[a:b] for a, b in zip(starts, starts[1:] + [len(data)], strict=True)]


def _split_config(au: bytes) -> tuple[bytes, bytes]:
    """Split the first access unit into (SPS+PPS, rest)."""
    sc = b"\x00\x00\x00\x01"
    nals = [sc + n for n in au.split(sc) if n]
    config = b"".join(n for n in nals if n[4] & 0x1F in (7, 8))
    rest = b"".join(n for n in nals if n[4] & 0x1F not in (7, 8))
    assert config and rest
    return config, rest


class FakeScrcpyServer:
    def __init__(self, port: int, access_units: list[bytes]) -> None:
        self.aus = access_units
        self.control = bytearray()
        self.stop = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", port))
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        video, _ = self.listener.accept()
        video.sendall(b"\x00" + b"Fake A56".ljust(64, b"\0") + struct.pack("!I", ph.CODEC_H264)
                      + struct.pack("!III", 0x80000000, PHONE_W, PHONE_H))
        control, _ = self.listener.accept()
        threading.Thread(target=self._read_control, args=(control,), daemon=True).start()
        pts = 0
        try:
            # MediaCodec delivers SPS/PPS as a separate "config" packet (flag bit 62).
            config, first = _split_config(self.aus[0])
            video.sendall(struct.pack("!QI", 1 << 62, len(config)) + config)
            aus = [first] + self.aus[1:]
            while not self.stop.is_set():
                for au in aus:
                    video.sendall(struct.pack("!QI", pts, len(au)) + au)
                    pts += 66_666
                    time.sleep(1 / 15)
                    if self.stop.is_set():
                        break
        except OSError:
            pass
        video.close()

    def _read_control(self, sock: socket.socket) -> None:
        while chunk := sock.recv(4096):
            self.control += chunk


class FakeAdb:
    def __init__(self, access_units: list[bytes]) -> None:
        self.aus = access_units
        self.server: FakeScrcpyServer | None = None
        self.calls: list[tuple] = []

    def state(self, serial):
        return "device"

    def devices(self):
        return []

    def run(self, *args, serial="", timeout=20.0):
        self.calls.append(args)
        if args[0] == "forward" and args[1] != "--remove":
            self.server = FakeScrcpyServer(int(args[1].split(":")[1]), self.aus)
        return subprocess.CompletedProcess(args, 0, "", "")

    def popen(self, *args, serial, stdout, stderr):
        self.calls.append(("popen",) + args)
        return subprocess.Popen(["sleep", "60"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_phone_video_reaches_the_car_and_touch_reaches_the_phone(tmp_path):
    adb = FakeAdb(_test_stream(tmp_path))
    session = Session(tmp_path / "0001")
    canvas = Canvas(320, 200)
    switch = DisplaySwitch(canvas)
    screen = StatusScreen(canvas, session=session, phone_status=lambda: "test")
    router = InputRouter(screen, switch)
    cfg = Config().phone
    cfg.start_app = "com.example.maps"
    link = PhoneLink = ph.PhoneLink(cfg, switch.new_video_frame(), switch, session=session,
                                    serial="fake:1", adb=adb)
    router.attach_phone(link.frame, link)
    vnc_port = _free_port()
    rfb = RfbServer(bind_address="127.0.0.1", port=vnc_port, canvas=switch, name="test",
                    session=session, screen=router, dump_dir=session.directory,
                    context_info=lambda: (1, 0x80, 0x00010001, 0))
    threads = [threading.Thread(target=f, daemon=True) for f in (link.run, rfb.serve_forever)]
    for t in threads:
        t.start()
    try:
        deadline = time.monotonic() + 20
        while not switch.showing(link.frame) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert switch.showing(link.frame), "no decoded phone frame arrived"
        colours = set(struct.unpack(f"<{320 * 200}H", bytes(link.frame.pixels)))
        assert len(colours) > 50                      # the test pattern, not a flat screen

        shot = tmp_path / "car.png"
        simulate_car.vnc_session(f"VNC://127.0.0.1:{vnc_port}", shot)
        assert shot.read_bytes().startswith(b"\x89PNG")
        time.sleep(0.5)
    finally:
        link.stop()
        rfb.stop()
        if adb.server:
            adb.server.stop.set()
        session.close()
    del PhoneLink

    # The scrcpy server was started for our fixed version with a car-sized display.
    popen = next(c for c in adb.calls if c[0] == "popen")
    assert ph.SCRCPY_VERSION in popen and "new_display=320x200/120" in popen
    # Control stream: start the app, then the simulator's tap in the middle of the car
    # screen (160,100) → (320,200) on the 640×400 phone display: DOWN then UP.
    ctl = bytes(adb.server.control)
    prefix = ph.start_app_message("com.example.maps") + ph.display_power_message(False)
    assert ctl.startswith(prefix)                 # start the app, phone screen off
    touches = ctl[len(prefix):]
    msgs = [touches[i:i + 32] for i in range(0, len(touches), 32)]
    decoded = [(m[1], struct.unpack_from("!iiHH", m, 10)) for m in msgs]
    assert decoded == [(ph.ACTION_DOWN, (320, 200, PHONE_W, PHONE_H)),
                       (ph.ACTION_UP, (320, 200, PHONE_W, PHONE_H))]
