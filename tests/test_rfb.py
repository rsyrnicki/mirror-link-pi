"""RFB server against clients that behave differently from our own simulator."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from mlpi.canvas import PALETTE, Canvas
from mlpi.rfb import RfbServer
from mlpi.session import STAGE_VNC_FRAMES, Session


def _recv(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        assert chunk, "server closed early"
        buf += chunk
    return buf


@pytest.fixture
def server(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    session = Session(tmp_path / "0001")
    canvas = Canvas(64, 32)
    srv = RfbServer(bind_address="127.0.0.1", port=port, canvas=canvas, name="t",
                    session=session, dump_dir=session.directory)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield srv, session, port
    srv.stop()
    session.close()


def _events(session: Session) -> list[dict]:
    session.sync()
    return [json.loads(line) for line in (session.directory / "events.jsonl").open()]


def _server_init(sock: socket.socket) -> tuple[int, int]:
    w, h = struct.unpack("!HH", _recv(sock, 4))
    _recv(sock, 16)
    (n,) = struct.unpack("!I", _recv(sock, 4))
    _recv(sock, n)
    return w, h


@pytest.mark.parametrize("version", [b"RFB 003.003\n", b"RFB 003.007\n"])
def test_old_protocol_versions(server, version):
    srv, session, port = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        assert _recv(sock, 12) == b"RFB 003.008\n"
        sock.sendall(version)
        if version == b"RFB 003.003\n":
            assert struct.unpack("!I", _recv(sock, 4))[0] == 1   # server picks None
        else:
            assert _recv(sock, 2) == b"\x01\x01"
            sock.sendall(b"\x01")                                # 3.7: no SecurityResult
        sock.sendall(b"\x01")
        assert _server_init(sock) == (64, 32)
        # default 32 bpp full frame
        sock.sendall(struct.pack("!BBHHHH", 3, 0, 0, 0, 64, 32))
        assert _recv(sock, 4)[0] == 0
        x, y, w, h, enc = struct.unpack("!HHHHi", _recv(sock, 12))
        assert (w, h, enc) == (64, 32, 0)
        assert len(_recv(sock, 64 * 32 * 4)) == 64 * 32 * 4
    time.sleep(0.2)
    assert session.stage == STAGE_VNC_FRAMES


def test_colour_map_client_gets_palette_first(server):
    srv, session, port = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        _recv(sock, 12)
        sock.sendall(b"RFB 003.008\n")
        _recv(sock, 2)
        sock.sendall(b"\x01")
        _recv(sock, 4)
        sock.sendall(b"\x01")
        _server_init(sock)
        pf = struct.pack("!BBBBHHHBBB3x", 8, 8, 0, 0, 0, 0, 0, 0, 0, 0)  # colour map
        sock.sendall(b"\x00\x00\x00\x00" + pf)
        sock.sendall(struct.pack("!BBHHHH", 3, 0, 0, 0, 64, 32))
        msg = _recv(sock, 6)
        assert msg[0] == 1                                   # SetColourMapEntries
        (count,) = struct.unpack("!H", msg[4:6])
        assert count == len(PALETTE)
        _recv(sock, 6 * count)
        assert _recv(sock, 4)[0] == 0                        # then the update
        _recv(sock, 12)
        assert len(_recv(sock, 64 * 32)) == 64 * 32


def test_mirrorlink_extension_and_unknown_messages_are_recorded(server):
    srv, session, port = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        _recv(sock, 12)
        sock.sendall(b"RFB 003.008\n")
        _recv(sock, 2)
        sock.sendall(b"\x01")
        _recv(sock, 4)
        sock.sendall(b"\x01")
        _server_init(sock)
        # SetEncodings incl. a guessed MirrorLink pseudo-encoding, then an ML message.
        sock.sendall(struct.pack("!BxH2i", 2, 2, 0, -523))
        sock.sendall(struct.pack("!BBH", 128, 3, 4) + b"\xde\xad\xbe\xef")
        sock.sendall(b"\xfa" + b"garbage")                   # unknown type 250
        sock.settimeout(6)
        assert sock.recv(10) == b""                           # server closes after dump
    time.sleep(0.3)
    events = _events(session)
    ml = [e for e in events if e["kind"] == "vnc_mirrorlink_msg"]
    assert ml and ml[0]["ext_type"] == 3 and ml[0]["payload_hex"] == "deadbeef"
    enc = [e for e in events if e["kind"] == "vnc_set_encodings"]
    assert enc[0]["encodings"] == [0, -523]
    assert any(e["kind"] == "vnc_unknown_msg" and e["msg_type"] == 250 for e in events)
    raw = (session.directory / "vnc-1-rx.bin").read_bytes()
    assert raw.endswith(b"\xfagarbage")
