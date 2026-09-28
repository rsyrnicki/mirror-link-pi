"""Minimal RFB (VNC) server that shows the StatusScreen canvas.

Replaces x11vnc + Xvfb so the Pi needs no extra packages, and — more importantly —
so that every byte the head unit sends on the VNC port is ours to record. If the car
ever connects, the raw dump (``vnc-N-rx.bin``) plus the decoded ``vnc_*`` events tell
us exactly what its MirrorLink VNC client expects next.

Implemented: RFB 3.3/3.7/3.8 handshake with security type None, ClientInit /
ServerInit, SetPixelFormat (8/16/32 bpp true colour and 8 bpp colour map),
SetEncodings, FramebufferUpdateRequest (Raw encoding), KeyEvent, PointerEvent,
ClientCutText.

MirrorLink (CCC-TS-010) adds extension messages on top of RFB. We do not know their
exact layout yet; client messages with type 128 are assumed to carry
``U8 extension-type, U16 payload-length`` and are logged, not answered. Anything
else unknown is dumped and the connection closed — the raw dump is the data we need.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path

from .canvas import (
    DEFAULT_PIXEL_FORMAT,
    Canvas,
    PixelEncoder,
    PixelFormat,
    UnsupportedPixelFormat,
    colour_map_entries,
)
from .session import STAGE_VNC_CONNECT, STAGE_VNC_FRAMES, Session

log = logging.getLogger(__name__)

SECURITY_NONE = 1
MSG_SET_PIXEL_FORMAT = 0
MSG_SET_ENCODINGS = 2
MSG_FB_UPDATE_REQUEST = 3
MSG_KEY = 4
MSG_POINTER = 5
MSG_CUT_TEXT = 6
MSG_MIRRORLINK = 128

ENCODING_NAMES = {
    0: "Raw", 1: "CopyRect", 2: "RRE", 5: "Hextile", 6: "zlib", 7: "Tight", 16: "ZRLE",
    -223: "DesktopSize", -224: "LastRect", -239: "Cursor", -257: "PointerPos",
    -308: "ExtendedDesktopSize", -523: "MirrorLink?", -524: "ContextInformation?",
    -525: "DesktopSize(ML)?", -526: "RunLengthEncoding(ML)?",
}


class _Closed(Exception):
    pass


class RfbServer:
    def __init__(self, *, bind_address: str, port: int, canvas: Canvas, name: str,
                 session: Session | None = None,
                 on_connect: Callable[[str], None] | None = None,
                 screen=None, dump_dir: Path | None = None,
                 dump_limit: int = 1_048_576) -> None:
        self.bind_address = bind_address
        self.port = port
        self.canvas = canvas
        self.name = name
        self.session = session
        self.on_connect = on_connect
        self.screen = screen
        self.dump_dir = dump_dir
        self.dump_limit = dump_limit
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._count = 0
        self.active = 0
        self._lock = threading.Lock()

    def serve_forever(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_address, self.port))
        self._sock.listen(4)
        self._sock.settimeout(1.0)
        log.info("RFB server listening on %s:%d (%dx%d)", self.bind_address, self.port,
                 self.canvas.width, self.canvas.height)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                log.warning("RFB accept failed: %s", exc)
                self._stop.wait(1.0)
                continue
            with self._lock:
                self._count += 1
                n = self._count
            threading.Thread(target=self._run_connection, args=(conn, addr, n),
                             name=f"rfb-{n}", daemon=True).start()
        self._sock.close()

    def _run_connection(self, conn: socket.socket, addr, n: int) -> None:
        with self._lock:
            self.active += 1
        try:
            RfbConnection(self, conn, addr, n).run()
        finally:
            with self._lock:
                self.active -= 1

    def stop(self) -> None:
        self._stop.set()


class RfbConnection:
    def __init__(self, server: RfbServer, sock: socket.socket, addr, number: int) -> None:
        self.server = server
        self.sock = sock
        self.peer = f"{addr[0]}:{addr[1]}"
        self.number = number
        self.session = server.session
        self.canvas = server.canvas
        self.pf = DEFAULT_PIXEL_FORMAT
        self.encoder = PixelEncoder(self.pf)
        self._send_colour_map = False
        self.encodings: list[int] = []
        self._alive = True
        self._req_cond = threading.Condition()
        self._request: tuple[bool, int, int, int, int] | None = None
        self._sent_version = -1
        self._send_lock = threading.Lock()
        self._counts: dict[int, int] = {}
        self._dump = None
        self._dumped = 0
        if server.dump_dir is not None:
            try:
                self._dump = open(server.dump_dir / f"vnc-{number}-rx.bin", "wb")
            except OSError as exc:
                log.warning("cannot open VNC dump file: %s", exc)

    # ----- logging -----

    def _event(self, kind: str, **fields) -> None:
        if self.session:
            self.session.event(kind, conn=self.number, peer=self.peer, **fields)

    def _should_log(self, msg_type: int) -> bool:
        """Rate-limit chatty message types (update requests, pointer moves)."""
        n = self._counts.get(msg_type, 0)
        return n <= 20 or n % 100 == 0

    # ----- socket io -----

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise _Closed("peer closed connection")
            buf += chunk
            self._record(chunk)
        return bytes(buf)

    def _record(self, chunk: bytes) -> None:
        if self._dump is None or self._dumped >= self.server.dump_limit:
            return
        part = chunk[:self.server.dump_limit - self._dumped]
        try:
            self._dump.write(part)
            self._dump.flush()
            self._dumped += len(part)
        except OSError:
            pass

    def _send(self, data: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(data)

    # ----- main flow -----

    def run(self) -> None:
        log.info("VNC connection #%d from %s", self.number, self.peer)
        self._event("vnc_connect")
        if self.session:
            self.session.reach(STAGE_VNC_CONNECT, peer=self.peer)
        if self.server.on_connect:
            try:
                self.server.on_connect(self.peer)
            except Exception:  # noqa: BLE001
                log.exception("on_connect hook failed")
        sender = None
        reason = "closed"
        try:
            self.sock.settimeout(30.0)
            self._handshake()
            self.sock.settimeout(None)
            sender = threading.Thread(target=self._sender_loop, name=f"rfb-{self.number}-tx",
                                      daemon=True)
            sender.start()
            self._message_loop()
        except _Closed as exc:
            reason = str(exc)
        except (OSError, TimeoutError) as exc:
            reason = f"socket error: {exc}"
        except Exception as exc:  # noqa: BLE001
            reason = f"server error: {exc!r}"
            log.exception("VNC connection #%d crashed", self.number)
        finally:
            self._alive = False
            with self._req_cond:
                self._req_cond.notify_all()
            try:
                self.sock.close()
            except OSError:
                pass
            if self._dump:
                self._dump.close()
            log.info("VNC connection #%d from %s ended: %s", self.number, self.peer, reason)
            self._event("vnc_disconnect", reason=reason, message_counts=self._counts,
                        rx_bytes_dumped=self._dumped)

    def _handshake(self) -> None:
        self._send(b"RFB 003.008\n")
        version = self._recv_exact(12)
        self._event("vnc_client_version", version=version.decode("latin-1"))
        try:
            minor = int(version[8:11])
            if not version.startswith(b"RFB 003."):
                raise ValueError
        except ValueError:
            raise _Closed(f"bad ProtocolVersion {version!r}") from None
        if minor < 7:
            self._send(struct.pack("!I", SECURITY_NONE))
        else:
            self._send(bytes([1, SECURITY_NONE]))
            chosen = self._recv_exact(1)[0]
            self._event("vnc_security", chosen=chosen)
            if chosen != SECURITY_NONE:
                if minor >= 8:
                    reason = b"only security type None is offered"
                    self._send(struct.pack("!I", 1) + struct.pack("!I", len(reason)) + reason)
                raise _Closed(f"client chose unsupported security type {chosen}")
            if minor >= 8:
                self._send(struct.pack("!I", 0))  # SecurityResult OK
        shared = self._recv_exact(1)[0]
        name = self.server.name.encode("utf-8")
        server_init = (struct.pack("!HH", self.canvas.width, self.canvas.height)
                       + self.pf.pack() + struct.pack("!I", len(name)) + name)
        self._send(server_init)
        self._event("vnc_handshake_done", rfb_minor=minor, shared=shared,
                    width=self.canvas.width, height=self.canvas.height,
                    server_pixel_format=self.pf.describe())
        if self.server.screen:
            self.server.screen.on_client(self.peer.split(":")[0], self.pf.describe())

    def _message_loop(self) -> None:
        while self._alive:
            msg_type = self._recv_exact(1)[0]
            self._counts[msg_type] = self._counts.get(msg_type, 0) + 1
            if msg_type == MSG_SET_PIXEL_FORMAT:
                data = self._recv_exact(19)
                self._set_pixel_format(PixelFormat.unpack(data[3:]))
            elif msg_type == MSG_SET_ENCODINGS:
                (count,) = struct.unpack("!xH", self._recv_exact(3))
                encs = list(struct.unpack(f"!{count}i", self._recv_exact(4 * count)))
                self.encodings = encs
                self._event("vnc_set_encodings", encodings=encs,
                            names=[ENCODING_NAMES.get(e, str(e)) for e in encs])
            elif msg_type == MSG_FB_UPDATE_REQUEST:
                inc, x, y, w, h = struct.unpack("!BHHHH", self._recv_exact(9))
                if self._should_log(msg_type):
                    self._event("vnc_update_request", incremental=inc, x=x, y=y, w=w, h=h)
                with self._req_cond:
                    self._request = (bool(inc), x, y, w, h)
                    self._req_cond.notify_all()
            elif msg_type == MSG_KEY:
                down, key = struct.unpack("!B2xI", self._recv_exact(7))
                self._event("vnc_key", down=down, keysym=f"0x{key:08x}")
                if self.server.screen:
                    self.server.screen.on_key(key, bool(down))
            elif msg_type == MSG_POINTER:
                mask, x, y = struct.unpack("!BHH", self._recv_exact(5))
                if self._should_log(msg_type) or mask:
                    self._event("vnc_pointer", buttons=mask, x=x, y=y)
                if self.server.screen:
                    self.server.screen.on_pointer(x, y, mask)
            elif msg_type == MSG_CUT_TEXT:
                (length,) = struct.unpack("!3xI", self._recv_exact(7))
                text = self._recv_exact(min(length, 1 << 20))
                self._event("vnc_cut_text", length=length, text=text[:512].decode("latin-1"))
            elif msg_type == MSG_MIRRORLINK:
                sub, length = struct.unpack("!BH", self._recv_exact(3))
                payload = self._recv_exact(length)
                self._event("vnc_mirrorlink_msg", ext_type=sub, length=length,
                            payload_hex=payload[:1024].hex())
                log.info("VNC MirrorLink extension message type %d (%d bytes)", sub, length)
            else:
                self._event("vnc_unknown_msg", msg_type=msg_type)
                log.warning("VNC unknown client message type %d — dumping and closing",
                            msg_type)
                self._drain_for(3.0)
                raise _Closed(f"unknown client message type {msg_type}")

    def _drain_for(self, seconds: float) -> None:
        self.sock.settimeout(seconds)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return
                self._record(chunk)
        except (TimeoutError, OSError):
            return

    def _set_pixel_format(self, pf: PixelFormat) -> None:
        try:
            encoder = PixelEncoder(pf)
        except UnsupportedPixelFormat as exc:
            self._event("vnc_set_pixel_format", format=pf.describe(), accepted=False,
                        error=str(exc))
            raise _Closed(f"unsupported pixel format: {exc}") from None
        with self._req_cond:
            self.pf = pf
            self.encoder = encoder
            self._send_colour_map = not pf.true_colour
            self._sent_version = -1   # force full repaint in the new format
        self._event("vnc_set_pixel_format", format=pf.describe(), accepted=True)
        if self.server.screen:
            self.server.screen.on_client(self.peer.split(":")[0], pf.describe())

    # ----- framebuffer updates -----

    def _sender_loop(self) -> None:
        first = True
        try:
            while self._alive:
                with self._req_cond:
                    while self._request is None and self._alive:
                        self._req_cond.wait(1.0)
                    if not self._alive:
                        return
                    req = self._request
                    encoder = self.encoder
                    colour_map = self._send_colour_map
                    self._send_colour_map = False
                    sent_version = self._sent_version
                if colour_map:
                    self._send(colour_map_entries())
                inc, rx, ry, rw, rh = req
                if inc and sent_version >= 0:
                    version, box = self.canvas.changes_since(sent_version)
                else:
                    version, box = self.canvas.version, (0, 0, self.canvas.width,
                                                         self.canvas.height)
                rect = _intersect(box, (rx, ry, rx + rw, ry + rh)) if box else None
                if rect is None:
                    with self.canvas.changed:
                        self.canvas.changed.wait(0.5)
                    continue
                with self._req_cond:
                    if self._request is req:
                        self._request = None
                    self._sent_version = version
                self._send_update(encoder, rect)
                if self.server.screen:
                    self.server.screen.on_frame()
                if first:
                    first = False
                    self._event("vnc_first_update", rect=rect, format=encoder.pf.describe())
                    if self.session:
                        self.session.reach(STAGE_VNC_FRAMES, peer=self.peer)
        except (OSError, _Closed) as exc:
            log.info("VNC sender #%d stopped: %s", self.number, exc)
            self._alive = False
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _send_update(self, encoder: PixelEncoder, rect: tuple[int, int, int, int]) -> None:
        x0, y0, x1, y1 = rect
        w, h = x1 - x0, y1 - y0
        pixels = self.canvas.encode_rect(encoder, x0, y0, w, h)
        header = struct.pack("!BxH", 0, 1) + struct.pack("!HHHHi", x0, y0, w, h, 0)
        self._send(header + pixels)


def _intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]
               ) -> tuple[int, int, int, int] | None:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0, y0, x1, y1)
