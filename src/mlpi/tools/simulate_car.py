"""Pretend to be the VW MIB II head unit.

Replays the exact request sequence recorded from the car on 2026-05-02
(captures/2026-05-02_session3-sai/launch-realcert.pcap), including the NOTIFY
callback listener, and then does what the car never did yet: connects to the
AppURI with an RFB client, requests a frame, sends a touch, and saves what it saw
as a PNG.

Use it at home against the Pi on the laptop's USB port (pre-flight check), or
against a server running on this machine during development.
"""

from __future__ import annotations

import http.client
import re
import socket
import struct
import sys
import threading
import time
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..variants import SIMULATOR_USER_AGENT

USER_AGENT = f"QNX/6.5.0, UPnP/1.0, MiniUPnPc/1.5 {SIMULATOR_USER_AGENT}"
_TM_APP = "urn:schemas-upnp-org:service:TmApplicationServer:1"
_TM_PROF = "urn:schemas-upnp-org:service:TmClientProfile:1"

# Bodies exactly as the car sent them (SOAP envelopes on one line).
_ENV = ('<?xml version="1.0"?>\r\n<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/'
        'envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '{}</s:Body></s:Envelope>\r\n')
CAR_PROFILE = (
    "&lt;clientProfile&gt;&lt;clientID&gt;VWAG_VOLKSWAGEN&lt;/clientID&gt;&lt;manufacturer&gt;"
    "VWAG_VOLKSWAGEN&lt;/manufacturer&gt;&lt;modelName&gt;VW-Mibstd2&lt;/modelName&gt;"
    "&lt;modelNumber&gt;0&lt;/modelNumber&gt;&lt;iconPreference&gt;&lt;mimetype&gt;image/png"
    "&lt;/mimetype&gt;&lt;width&gt;100&lt;/width&gt;&lt;height&gt;100&lt;/height&gt;&lt;depth&gt;"
    "8&lt;/depth&gt;&lt;/iconPreference&gt;&lt;connectivity&gt;&lt;bluetooth&gt;&lt;bdAddr&gt;"
    "64d4bdd2125c&lt;/bdAddr&gt;&lt;startConnection&gt;0&lt;/startConnection&gt;&lt;/bluetooth"
    "&gt;&lt;/connectivity&gt;&lt;rtpStreaming&gt;&lt;payloadType&gt;98,99&lt;/payloadType&gt;"
    "&lt;audioIPL&gt;4800&lt;/audioIPL&gt;&lt;audioMPL&gt;9600&lt;/audioMPL&gt;&lt;/rtpStreaming"
    "&gt;&lt;/clientProfile&gt;"
)
REQUESTS = {
    "GetClientProfile": (_TM_PROF, f'<u:GetClientProfile xmlns:u="{_TM_PROF}">'
                         '<ProfileID>0</ProfileID></u:GetClientProfile>'),
    "SetClientProfile": (_TM_PROF, f'<u:SetClientProfile xmlns:u="{_TM_PROF}"><ProfileID>0'
                         f'</ProfileID><ClientProfile>{CAR_PROFILE}</ClientProfile>'
                         '</u:SetClientProfile>'),
    "GetApplicationList": (_TM_APP, f'<u:GetApplicationList xmlns:u="{_TM_APP}">'
                           '<AppListingFilter>*</AppListingFilter><ProfileID>0</ProfileID>'
                           '</u:GetApplicationList>'),
    "LaunchApplication": (_TM_APP, f'<u:LaunchApplication xmlns:u="{_TM_APP}"><AppID>'
                          '0x00000001</AppID><ProfileID>0</ProfileID></u:LaunchApplication>'),
    "GetApplicationStatus(0x1)": (_TM_APP, f'<u:GetApplicationStatus xmlns:u="{_TM_APP}">'
                                  '<AppID>0x1</AppID></u:GetApplicationStatus>'),
    "GetApplicationStatus": (_TM_APP, f'<u:GetApplicationStatus xmlns:u="{_TM_APP}">'
                             '<AppID>0x00000001</AppID></u:GetApplicationStatus>'),
}


class SimulationError(RuntimeError):
    pass


# ---------- UPnP side ----------

class _NotifyHandler(BaseHTTPRequestHandler):
    def do_NOTIFY(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        self.server.notifications.append((self.headers.get("SEQ"), body))  # type: ignore
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def _local_ip_towards(target: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def _http(target: str, port: int, method: str, path: str, body: bytes = b"",
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(target, port, timeout=10)
    hdrs = {"User-Agent": USER_AGENT, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    hdrs.update(headers or {})
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    out = resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    conn.close()
    return out


def _soap(target: str, port: int, name: str) -> str:
    urn, inner = REQUESTS[name]
    action = name.split("(")[0]
    path = "//ctrl/TmClientProfile" if urn == _TM_PROF else "//ctrl/TmApplicationServer"
    status, _, data = _http(target, port, "POST", path, _ENV.format(inner).encode(), {
        "Content-Type": "text/xml", "SOAPAction": f'"{urn}#{action}"'})
    text = data.decode("utf-8", "replace")
    if status != 200:
        raise SimulationError(f"{name} → HTTP {status}: {text[:300]}")
    return text


def _arg(text: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return unescape(m.group(1)) if m else ""


def upnp_handshake(target: str, port: int, callback_ip: str) -> str:
    """Run one car-style attempt. Returns the AppURI."""
    notify_server = ThreadingHTTPServer((callback_ip, 0), _NotifyHandler)
    notify_server.notifications = []  # type: ignore[attr-defined]
    threading.Thread(target=notify_server.serve_forever, daemon=True).start()
    try:
        status, _, desc = _http(target, port, "GET", "/")
        if status != 200:
            raise SimulationError(f"descriptor → HTTP {status}")
        desc_text = desc.decode()
        mlv = re.search(r"<X_mirrorLinkVersion>.*?</X_mirrorLinkVersion>", desc_text)
        print(f"  descriptor OK ({len(desc)} bytes), X_mirrorLinkVersion: "
              f"{mlv.group(0) if mlv else 'absent'}")
        _http(target, port, "GET", "//scpd/TmApplicationServer.xml")

        _soap(target, port, "GetClientProfile")
        result = _arg(_soap(target, port, "SetClientProfile"), "ResultProfile")
        if not result.startswith("<clientProfile>"):
            raise SimulationError(f"ResultProfile is not the profile XML: {result[:80]!r}")
        print("  SetClientProfile echoed a well-formed profile")
        app_list = _arg(_soap(target, port, "GetApplicationList"), "AppListing")
        print(f"  AppListing: {app_list[:160]}...")

        cb_port = notify_server.server_address[1]
        status, headers, _ = _http(target, port, "SUBSCRIBE", "//evt/TmApplicationServer",
                                   headers={"CALLBACK": f"<http://{callback_ip}:{cb_port}/>",
                                            "NT": "upnp:event", "TIMEOUT": "Second-1800"})
        if status != 200 or "sid" not in headers:
            raise SimulationError(f"SUBSCRIBE → HTTP {status} {headers}")
        _soap(target, port, "GetApplicationList")
        app_uri = _arg(_soap(target, port, "LaunchApplication"), "AppURI")
        print(f"  LaunchApplication → AppURI {app_uri}")
        status_xml = _arg(_soap(target, port, "GetApplicationStatus(0x1)"), "AppStatus")
        if "Foreground" not in status_xml:
            raise SimulationError(f"app not Foreground after launch: {status_xml}")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(notify_server.notifications) < 2:
            time.sleep(0.05)
        print(f"  NOTIFY received: {len(notify_server.notifications)} "
              f"{[n[1][-80:] for n in notify_server.notifications]}")
        _soap(target, port, "GetApplicationStatus")
        return app_uri
    finally:
        notify_server.shutdown()


# ---------- VNC side ----------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SimulationError("VNC server closed the connection")
        buf += chunk
    return bytes(buf)


def _read_update(sock: socket.socket, fb: bytearray, width: int) -> int:
    msg_type = _recv_exact(sock, 1)[0]
    while msg_type == 1:  # SetColourMapEntries: skip
        _, n = struct.unpack("!xHH", _recv_exact(sock, 5))
        _recv_exact(sock, 6 * n)
        msg_type = _recv_exact(sock, 1)[0]
    if msg_type != 0:
        raise SimulationError(f"unexpected server message type {msg_type}")
    (count,) = struct.unpack("!xH", _recv_exact(sock, 3))
    for _ in range(count):
        x, y, w, h, enc = struct.unpack("!HHHHi", _recv_exact(sock, 12))
        if enc != 0:
            raise SimulationError(f"unexpected encoding {enc}")
        data = _recv_exact(sock, w * h * 2)
        for row in range(h):
            for col in range(w):
                (v,) = struct.unpack_from("<H", data, (row * w + col) * 2)
                r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
                o = ((y + row) * width + x + col) * 3
                fb[o:o + 3] = bytes(((r * 255) // 31, (g * 255) // 63, (b * 255) // 31))
    return count


def vnc_session(app_uri: str, screenshot: Path) -> None:
    m = re.match(r"(?i)vnc://([^:/]+):(\d+)", app_uri)
    if not m:
        raise SimulationError(f"cannot parse AppURI {app_uri!r}")
    host, port = m.group(1), int(m.group(2))
    sock = socket.create_connection((host, port), timeout=10)
    try:
        version = _recv_exact(sock, 12)
        sock.sendall(b"RFB 003.008\n")
        n_types = _recv_exact(sock, 1)[0]
        types = _recv_exact(sock, n_types)
        if 1 not in types:
            raise SimulationError(f"no None security offered: {list(types)}")
        sock.sendall(b"\x01")
        if struct.unpack("!I", _recv_exact(sock, 4))[0] != 0:
            raise SimulationError("security handshake failed")
        sock.sendall(b"\x01")  # shared
        width, height = struct.unpack("!HH", _recv_exact(sock, 4))
        _recv_exact(sock, 16)
        (name_len,) = struct.unpack("!I", _recv_exact(sock, 4))
        name = _recv_exact(sock, name_len).decode()
        print(f"  VNC {version.decode().strip()} '{name}' {width}x{height}")
        # Ask for RGB565 little-endian, as embedded head units commonly do.
        pf = struct.pack("!BBBBHHHBBB3x", 16, 16, 0, 1, 31, 63, 31, 11, 5, 0)
        sock.sendall(b"\x00\x00\x00\x00" + pf)
        sock.sendall(struct.pack("!BxHi", 2, 1, 0))
        sock.sendall(struct.pack("!BBHHHH", 3, 0, 0, 0, width, height))
        fb = bytearray(width * height * 3)
        _read_update(sock, fb, width)
        # Touch the middle of the screen and wait for the echo.
        sock.sendall(struct.pack("!BBHH", 5, 1, width // 2, height // 2))
        sock.sendall(struct.pack("!BBHH", 5, 0, width // 2, height // 2))
        time.sleep(1.2)
        sock.sendall(struct.pack("!BBHHHH", 3, 1, 0, 0, width, height))
        _read_update(sock, fb, width)
    finally:
        sock.close()
    from ..http_descriptor import encode_png
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        rows += fb[y * width * 3:(y + 1) * width * 3]
    screenshot.write_bytes(encode_png(width, height, bytes(rows)))
    print(f"  saved what the car would see → {screenshot}")


def run(*, target: str, http_port: int = 8080, callback_ip: str = "", vnc: bool = True,
        screenshot: Path = Path("car-view.png"), attempts: int = 1) -> int:
    callback_ip = callback_ip or _local_ip_towards(target)
    try:
        for n in range(1, attempts + 1):
            if n > 1:
                time.sleep(5)  # > attempt_gap_seconds: the server sees a new attempt
            print(f"attempt {n}/{attempts} against {target}:{http_port} "
                  f"(callback via {callback_ip})")
            app_uri = upnp_handshake(target, http_port, callback_ip)
            if vnc and n == attempts:
                vnc_session(app_uri, screenshot)
    except (SimulationError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print("OK")
    return 0
