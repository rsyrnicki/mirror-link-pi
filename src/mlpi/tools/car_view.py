"""A live window that talks to the Pi exactly like the VW head unit does.

Desktop VNC viewers ask for 24/32-bit or 15-bit colour, so the Pi has to convert
every frame in Python, which the car never needs (it asks for RGB565, the Pi's native
format). They also use their own update pacing. That made desk tests laggier than the
car. ``mlpi car-view`` does the car's MirrorLink VNC handshake, asks for RGB565 raw
pixels, keeps one incremental update request outstanding like the car, and shows the
picture in a small Tk window. Clicks and drags become touches.

It also measures: frames per second, throughput, and "tap → screen", the time from a
press to the next screen update (a rough upper bound for touch latency).

    PYTHONPATH=src python3 -m mlpi car-view --target 192.168.7.2
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time

from .. import mirrorlink_vnc as ml
from ..canvas import PixelFormat
from ..video import Rgb565Converter
from .simulate_car import SimulationError, _recv_exact, open_mirrorlink_vnc

# RGB565 → bytes R, G, B, X (little-endian 32-bit, red lowest)
_RGBX = PixelFormat(32, 24, False, True, 255, 255, 255, 0, 8, 16)


class CarClient:
    """The car's side of the VNC session: receives updates into an RGB565 buffer."""

    def __init__(self, host: str, port: int = 5900, *, verbose: bool = True) -> None:
        self.sock, self.width, self.height = open_mirrorlink_vnc(host, port, verbose=verbose)
        self.sock.settimeout(None)
        self.fb = bytearray(self.width * self.height * 2)
        self.lock = threading.Lock()          # fb
        self._send_lock = threading.Lock()
        self.version = 0
        self.updates = 0
        self.bytes = 0
        self.error: str = ""
        self._press_at: float | None = None
        self.tap_ms: list[float] = []
        self._alive = True
        self._rgbx = Rgb565Converter(_RGBX)

    # ----- sending -----

    def _send(self, data: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(data)

    def request(self, incremental: bool) -> None:
        self._send(struct.pack("!BBHHHH", 3, int(incremental), 0, 0, self.width, self.height))

    def pointer(self, buttons: int, x: int, y: int) -> None:
        x = max(0, min(self.width - 1, x))
        y = max(0, min(self.height - 1, y))
        if buttons and self._press_at is None:
            self._press_at = time.monotonic()
        try:
            self._send(struct.pack("!BBHH", 5, buttons, x, y))
        except OSError as exc:
            self.error = str(exc)

    # ----- receiving -----

    def run(self) -> None:
        try:
            self.request(False)
            while self._alive:
                msg = _recv_exact(self.sock, 1)[0]
                if msg == 0:
                    self._read_update()
                    self.request(True)       # one request outstanding, like the car
                elif msg == 1:               # SetColourMapEntries
                    _, n = struct.unpack("!xHH", _recv_exact(self.sock, 5))
                    _recv_exact(self.sock, 6 * n)
                elif msg == 2:               # Bell
                    pass
                elif msg == 3:               # ServerCutText
                    (n,) = struct.unpack("!3xI", _recv_exact(self.sock, 7))
                    _recv_exact(self.sock, n)
                elif msg == ml.MSG_MIRRORLINK:
                    _ext, n = struct.unpack("!BH", _recv_exact(self.sock, 3))
                    _recv_exact(self.sock, n)
                else:
                    raise SimulationError(f"unexpected server message type {msg}")
        except (OSError, SimulationError) as exc:
            if self._alive:
                self.error = str(exc)
        finally:
            self._alive = False

    def _read_update(self) -> None:
        (count,) = struct.unpack("!xH", _recv_exact(self.sock, 3))
        stride = self.width * 2
        for _ in range(count):
            x, y, w, h, enc = struct.unpack("!HHHHi", _recv_exact(self.sock, 12))
            if enc == ml.ENC_CONTEXT_INFO:
                _recv_exact(self.sock, 20)
                continue
            if enc != 0:
                raise SimulationError(f"unexpected encoding {enc}")
            data = _recv_exact(self.sock, w * h * 2)
            self.bytes += len(data)
            with self.lock:
                if x == 0 and w == self.width:
                    self.fb[y * stride:(y + h) * stride] = data
                else:
                    for r in range(h):
                        o = (y + r) * stride + 2 * x
                        self.fb[o:o + 2 * w] = data[r * 2 * w:(r + 1) * 2 * w]
        with self.lock:
            self.version += 1
            self.updates += 1
        if self._press_at is not None:
            self.tap_ms.append((time.monotonic() - self._press_at) * 1000)
            self._press_at = None

    def rgb(self) -> bytes:
        """The current picture as packed RGB (for a PPM)."""
        with self.lock:
            rgbx = self._rgbx.convert(bytes(self.fb))
        n = self.width * self.height
        out = bytearray(n * 3)
        out[0::3] = rgbx[0::4]
        out[1::3] = rgbx[1::4]
        out[2::3] = rgbx[2::4]
        return bytes(out)

    def stop(self) -> None:
        if self._alive:
            self._alive = False
            try:
                self._send(ml.byebye())
            except OSError:
                pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class _Stats:
    def __init__(self, client: CarClient) -> None:
        self.client = client
        self.t = time.monotonic()
        self.updates = 0
        self.bytes = 0

    def line(self) -> str:
        c, now = self.client, time.monotonic()
        dt = max(1e-6, now - self.t)
        fps = (c.updates - self.updates) / dt
        mbs = (c.bytes - self.bytes) / dt / 1e6
        self.t, self.updates, self.bytes = now, c.updates, c.bytes
        taps = c.tap_ms[-10:]
        tap = f", tap → screen {sorted(taps)[len(taps) // 2]:.0f} ms" if taps else ""
        return f"{fps:.1f} updates/s, {mbs:.1f} MB/s{tap}"


def run(*, target: str, port: int = 5900, seconds: float = 0, window: bool = True) -> int:
    try:
        client = CarClient(target, port)
    except (OSError, SimulationError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    receiver = threading.Thread(target=client.run, name="car-view", daemon=True)
    receiver.start()
    stats = _Stats(client)
    try:
        if window:
            _window(client, stats, seconds)
        else:
            end = time.monotonic() + (seconds or 10)
            while time.monotonic() < end and client._alive:
                time.sleep(min(5, max(0.1, end - time.monotonic())))
                print(f"  {stats.line()}")
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
    if client.error:
        print(f"connection ended: {client.error}", file=sys.stderr)
    return 0 if client.updates else 1


def _window(client: CarClient, stats: _Stats, seconds: float) -> None:
    try:
        import tkinter as tk
    except ImportError:
        raise SystemExit("car-view needs Tk: sudo apt install python3-tk") from None
    root = tk.Tk()
    root.title("MirrorLink-Pi car view")
    root.resizable(False, False)
    header = b"P6 %d %d 255\n" % (client.width, client.height)
    image = tk.PhotoImage(data=header + client.rgb(), format="PPM")
    label = tk.Label(root, image=image, borderwidth=0, cursor="hand2")
    label.pack()
    state = {"shown": -1, "buttons": 0}

    def refresh() -> None:
        if not client._alive:
            root.title(f"MirrorLink-Pi car view — disconnected {client.error}")
            return
        if client.version != state["shown"]:
            state["shown"] = client.version
            image.configure(data=header + client.rgb(), format="PPM")
        root.after(15, refresh)

    def report() -> None:
        line = stats.line()
        print(f"  {line}")
        root.title(f"MirrorLink-Pi car view — {line}")
        root.after(5000, report)

    def press(e) -> None:
        state["buttons"] = 1
        client.pointer(1, e.x, e.y)

    def drag(e) -> None:
        client.pointer(state["buttons"], e.x, e.y)

    def release(e) -> None:
        state["buttons"] = 0
        client.pointer(0, e.x, e.y)

    label.bind("<ButtonPress-1>", press)
    label.bind("<B1-Motion>", drag)
    label.bind("<ButtonRelease-1>", release)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    if seconds:
        root.after(int(seconds * 1000), root.destroy)
    root.after(15, refresh)
    root.after(5000, report)
    root.mainloop()
