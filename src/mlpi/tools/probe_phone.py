"""Probe a real MirrorLink phone (e.g. a Galaxy S6) as a reference implementation.

The laptop plays the MirrorLink Client (car). It wakes the phone with the MirrorLink
USB command, gets an address from the phone's DHCP server, discovers it over SSDP,
then records what a certified MirrorLink server actually sends:

  device-description.xml   the phone's UPnP device XML (incl. X_Signature, versions)
  scpd/*.xml               its service descriptions
  app-list.xml             its GetApplicationList response (categories, trust levels)
  dap/attestation.xml      its DAP attestationResponse (real CCC-chained certificates)
  dap/*.der + certs.txt    those certificates, decoded (validity dates = is it expired?)
  phone-screen.png         a VNC screenshot of the phone's MirrorLink screen
  events.jsonl / report    the full timeline

This does not defeat or copy anything protected — it records public protocol data
from a device you own, to learn where a certified server differs from ours and at
which step a head unit would enforce certification. See docs/spec-notes.md.

Root is required (usbfs control transfer + configuring the network interface).
"""

from __future__ import annotations

import base64
import http.client
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from html import unescape
from pathlib import Path

from .. import dhcp, ssdp, usbhost
from .. import mirrorlink_vnc as ml
from ..canvas import PixelFormat
from ..config import Config
from ..session import Session

_TM_PROF = "urn:schemas-upnp-org:service:TmClientProfile:1"
_TM_APP = "urn:schemas-upnp-org:service:TmApplicationServer:1"
USER_AGENT = "mlpi-probe-phone/0.1 (MirrorLink client)"

# A minimal but valid client profile we present to the phone (we are the car).
CLIENT_PROFILE = (
    '<clientProfile xmlns="urn:schemas-upnp-org:tmclientprofile:clientprofile-1-0">'
    "<clientID>mlpi-probe</clientID><manufacturer>mlpi</manufacturer>"
    "<modelName>probe-phone</modelName><modelNumber>0</modelNumber>"
    "<mirrorLinkVersion><majorVersion>1</majorVersion><minorVersion>1</minorVersion>"
    "</mirrorLinkVersion>"
    "<presentations><presentation>vncu</presentation></presentations>"
    "</clientProfile>")


class ProbeError(RuntimeError):
    pass


# ---------- USB + network bring-up ----------

def wait_for_interface(before: set[str], timeout: float = 30.0) -> str:
    """Return the name of the network interface that appears after the USB command."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = {p.name for p in Path("/sys/class/net").glob("*")}
        new = now - before - {"lo"}
        # Prefer usb-ish names, else any new interface.
        for name in sorted(new, key=lambda n: (not n.startswith(("usb", "ncm", "enx", "eth")), n)):
            return name
        time.sleep(0.5)
    raise ProbeError("no new network interface appeared after the MirrorLink USB command "
                     "— did the phone switch to MirrorLink mode? (enable it in phone "
                     "settings, accept any prompt on the phone, then run again)")


def _release_interface(interface: str) -> None:
    """Stop NetworkManager / dhcpcd from racing our DHCP client on this interface."""
    for cmd in (["nmcli", "device", "set", interface, "managed", "no"],
                ["dhcpcd", "-x", interface]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    subprocess.run(["ip", "addr", "flush", "dev", interface], capture_output=True)


def _bring_up(interface: str, timeout: float = 5.0) -> None:
    """Set the link up and wait for it: unmanaging it in NetworkManager can leave it down."""
    subprocess.run(["ip", "link", "set", interface, "up"], capture_output=True)
    state = Path("/sys/class/net") / interface / "operstate"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if state.read_text().strip() in ("up", "unknown"):
                return
        except OSError:
            pass
        time.sleep(0.2)


class LinkWatcher:
    """Record every frame on the phone's interface into phone.pcap, and remember what
    the phone sent on its own — so a run that fails still says why (does the phone have
    an IPv4 address? is it asking *us* for one? is it IPv6 only?)."""

    PACKET_OUTGOING = 4

    def __init__(self, interface: str, pcap_path: Path) -> None:
        self.interface = interface
        self.pcap_path = pcap_path
        self.phone_ipv4: set[str] = set()
        self.phone_macs: set[str] = set()
        self.phone_dhcp_discover = False
        self.ethertypes: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        from ..capture import ETH_P_ALL, PcapWriter
        writer = PcapWriter(self.pcap_path)
        sock = None
        try:
            while not self._stop.is_set():
                if sock is None:
                    try:
                        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                             socket.htons(ETH_P_ALL))
                        sock.bind((self.interface, 0))
                        sock.settimeout(0.5)
                    except OSError:
                        if sock:
                            sock.close()
                        sock = None
                        self._stop.wait(0.5)
                        continue
                try:
                    frame, addr = sock.recvfrom(262144)
                except TimeoutError:
                    continue
                except OSError:
                    sock.close()
                    sock = None
                    continue
                writer.write(frame)
                if addr[2] != self.PACKET_OUTGOING:
                    self.observe(frame)
        finally:
            if sock:
                sock.close()
            writer.close()

    def observe(self, frame: bytes) -> None:
        """Learn from one frame the phone sent (Ethernet II)."""
        if len(frame) < 14:
            return
        (etype,) = struct.unpack_from("!H", frame, 12)
        self.ethertypes[etype] = self.ethertypes.get(etype, 0) + 1
        self.phone_macs.add(frame[6:12].hex(":"))
        if etype == 0x0806 and len(frame) >= 42:            # ARP: sender protocol address
            self._add_ip(frame[28:32])
        elif etype == 0x0800 and len(frame) >= 34:          # IPv4
            ihl = (frame[14] & 0x0F) * 4
            self._add_ip(frame[26:30])
            if frame[23] == 17 and len(frame) >= 14 + ihl + 4:
                sport, dport = struct.unpack_from("!HH", frame, 14 + ihl)
                if (sport, dport) == (68, 67):
                    self.phone_dhcp_discover = True

    def _add_ip(self, raw: bytes) -> None:
        ip = socket.inet_ntoa(raw)
        if ip != "0.0.0.0" and not ip.startswith(("169.254.", "255.")):
            self.phone_ipv4.add(ip)

    def summary(self) -> str:
        names = {0x0800: "IPv4", 0x0806: "ARP", 0x86DD: "IPv6"}
        kinds = ", ".join(f"{names.get(t, hex(t))}×{n}" for t, n in sorted(self.ethertypes.items()))
        return (f"phone sent: {kinds or 'nothing'}; IPv4 addresses: "
                f"{sorted(self.phone_ipv4) or 'none'}; DHCP requests: "
                f"{'yes' if self.phone_dhcp_discover else 'no'}")


def neighbour_address(phone_ip: str) -> str:
    """An address for us in the phone's /24 that isn't the phone's."""
    a, b, c, d = phone_ip.split(".")
    return f"{a}.{b}.{c}.{201 if d == '200' else 200}"


# When the phone turns out to be a DHCP *client*, we serve it from this subnet
# (MirrorLink Part 1 §5.4.1: 192.168.x.y with x = 2…127).
_SERVE_IP, _SERVE_CLIENT = "192.168.8.1", "192.168.8.44"


def get_addresses(iface: str, mac: bytes, watcher: LinkWatcher,
                  session: Session) -> tuple[str, str]:
    """Return (our address, phone address) on ``iface``.

    Spec way first (the phone is the DHCP server). If it never offers, fall back on
    what the phone sends by itself: an IPv4 address we can join, or DHCP requests we
    can answer."""
    try:
        lease = dhcp.dhcp_client(iface, mac, timeout=30.0,
                                 on_event=lambda kind, **f: session.event(kind, **f))
        print(f"  got {lease['address']} from phone at {lease['server']}")
        return lease["address"], lease["server"]
    except TimeoutError as exc:
        print(f"  {exc}; looking at what the phone sends instead")
    print(f"  {watcher.summary()}")
    session.event("dhcp_fallback", summary=watcher.summary(),
                  phone_ipv4=sorted(watcher.phone_ipv4), phone_macs=sorted(watcher.phone_macs),
                  phone_dhcp_discover=watcher.phone_dhcp_discover)

    if watcher.phone_ipv4:
        phone = sorted(watcher.phone_ipv4)[0]
        ours = neighbour_address(phone)
        subprocess.run(["ip", "addr", "replace", f"{ours}/24", "dev", iface], check=False)
        print(f"  phone uses {phone}: joining its subnet as {ours}")
        return ours, phone

    if watcher.phone_dhcp_discover:
        print(f"  the phone asks US for an address: serving {_SERVE_CLIENT}")
        subprocess.run(["ip", "addr", "replace", f"{_SERVE_IP}/24", "dev", iface], check=False)
        server = dhcp.DhcpServer(interface=iface, server_ip=_SERVE_IP, prefix=24,
                                 client_ip=_SERVE_CLIENT, offer_router=False,
                                 offer_dns=False, session=session)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            leased = list(server._leases.values())
            if leased and leased[0] in watcher.phone_ipv4:
                print(f"  phone took {leased[0]}")
                return _SERVE_IP, leased[0]
            time.sleep(0.5)
        raise ProbeError(f"phone asked for an address but never used ours ({watcher.summary()})")

    raise ProbeError(f"no DHCP offer and no IPv4 traffic from the phone ({watcher.summary()}). "
                     "phone.pcap in the recording has everything it did send.")


def interface_mac(interface: str) -> bytes:
    text = (Path("/sys/class/net") / interface / "address").read_text().strip()
    return bytes(int(b, 16) for b in text.split(":"))


# ---------- HTTP (as the MirrorLink client) ----------

def _http(host: str, port: int, method: str, path: str, body: bytes = b"",
          headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=15)
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    conn.close()
    return status, data


def _soap(host: str, port: int, control_path: str, urn: str, action: str, inner: str) -> str:
    envelope = ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'{inner}</s:Body></s:Envelope>')
    status, data = _http(host, port, "POST", control_path, envelope.encode(),
                         {"Content-Type": 'text/xml; charset="utf-8"',
                          "SOAPAction": f'"{urn}#{action}"'})
    text = data.decode("utf-8", "replace")
    if status != 200:
        raise ProbeError(f"{action} → HTTP {status}: {text[:300]}")
    return text


def _arg(xml: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.DOTALL)
    return unescape(m.group(1)) if m else ""


def _control_path(descriptor: str, service_urn: str) -> str:
    """Find the controlURL for a service in the device descriptor."""
    block = re.search(
        rf"<service>(?:(?!</service>).)*?{re.escape(service_urn)}.*?</service>",
        descriptor, re.DOTALL)
    if block:
        url = re.search(r"<controlURL>\s*([^<]+?)\s*</controlURL>", block.group(0))
        if url:
            return url.group(1)
    return ""


# ---------- DAP client (records the phone's real certificates) ----------

def run_dap(host: str, dap_uri: str, session: Session, out: Path) -> None:
    m = re.match(r"(?i)dap://([^:/]+):(\d+)", dap_uri)
    if not m:
        raise ProbeError(f"cannot parse DAP URI {dap_uri!r}")
    nonce = base64.b64encode(b"mlpi-probe-nonce-01").decode()
    request = (
        '<attestationRequest><version><majorVersion>1</majorVersion>'
        '<minorVersion>1</minorVersion></version>'
        '<trustRoot>AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=</trustRoot>'
        f'<nonce>{nonce}</nonce><componentID>*</componentID></attestationRequest>')
    with socket.create_connection((m.group(1), int(m.group(2))), timeout=15) as sock:
        sock.sendall(request.encode())
        sock.settimeout(15.0)
        buf = b""
        while b"</attestationResponse>" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    text = buf.decode("utf-8", "replace")
    (out / "attestation.xml").write_text(text)
    result = _arg(text, "result")
    session.event("dap_response", result=result, bytes=len(buf))
    print(f"  DAP result: {result}  (0 = success — a working certified attestation!)")
    _save_certificates(text, session, out)


def _save_certificates(xml: str, session: Session, out: Path) -> None:
    summary = []
    certs = [("device", m) for m in re.findall(r"<deviceCertificate>(.*?)</deviceCertificate>",
                                               xml, re.DOTALL)]
    certs += [("manufacturer", m) for m in
              re.findall(r"<manufacturerCertificate>(.*?)</manufacturerCertificate>", xml,
                         re.DOTALL)]
    for i, (kind, b64) in enumerate(certs):
        try:
            der = base64.b64decode("".join(b64.split()))
        except (ValueError, TypeError):
            continue
        path = out / f"{kind}-{i}.der"
        path.write_bytes(der)
        info = _openssl_summary(path)
        summary.append(f"=== {path.name} ({len(der)} bytes) ===\n{info}")
        session.event("dap_certificate", cert_kind=kind, file=path.name, bytes=len(der),
                      **_cert_fields(info))
    if summary:
        (out / "certs.txt").write_text("\n\n".join(summary))
        print(f"  saved {len(certs)} certificate(s) → {out}/certs.txt")
    else:
        print("  no certificates in the DAP response (attestation likely failed)")


def _openssl_summary(der: Path) -> str:
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-in", str(der), "-noout",
             "-subject", "-issuer", "-dates", "-fingerprint", "-sha256"],
            capture_output=True, text=True, timeout=10)
        return proc.stdout or proc.stderr
    except (OSError, subprocess.SubprocessError):
        return "(openssl not available — DER saved for offline analysis)"


def _cert_fields(info: str) -> dict[str, str]:
    out = {}
    for line in info.splitlines():
        key, _, value = line.partition("=")
        if key in ("subject", "issuer", "notBefore", "notAfter"):
            out[key] = value.strip()
    return out


# ---------- VNC client (records the phone's screen) ----------

def _recv(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProbeError("phone's VNC server closed the connection")
        buf += chunk
    return bytes(buf)


def vnc_screenshot(host: str, vnc_uri: str, session: Session, out: Path) -> None:
    m = re.match(r"(?i)vnc://([^:/]+):(\d+)", vnc_uri)
    if not m:
        raise ProbeError(f"cannot parse VNC URI {vnc_uri!r}")
    port = int(m.group(2))
    raw = out / "vnc-rx.bin"
    dump = raw.open("wb")
    sock = socket.create_connection((m.group(1), port), timeout=15)
    try:
        server_version = _recv(sock, 12)
        dump.write(server_version)
        sock.sendall(b"RFB 003.008\n")
        n_types = _recv(sock, 1)[0]
        types = _recv(sock, n_types)
        dump.write(bytes([n_types]) + types)
        if 1 not in types:
            raise ProbeError(f"phone offers no None security ({list(types)}); needs auth")
        sock.sendall(b"\x01")
        if struct.unpack("!I", _recv(sock, 4))[0] != 0:
            raise ProbeError("VNC security handshake failed")
        sock.sendall(b"\x01")  # ClientInit shared
        init = _recv(sock, 24)
        width, height = struct.unpack("!HH", init[:4])
        server_pf = PixelFormat.unpack(init[4:20])
        (name_len,) = struct.unpack("!I", init[20:24])
        name = _recv(sock, name_len).decode("latin-1")
        session.event("vnc_server_init", width=width, height=height, name=name,
                      pixel_format=server_pf.describe())
        print(f"  phone VNC '{name}' {width}x{height}, {server_pf.describe()}")

        # Announce MirrorLink support; answer the phone's Server*Configuration.
        sock.sendall(struct.pack("!BxH3i", 2, 3, 0, ml.ENC_MIRRORLINK, ml.ENC_CONTEXT_INFO))
        pixel_formats = _drain_ml_config(sock, dump, session)
        want_565 = ml.PF_RGB565 & (pixel_formats if pixel_formats else ml.PF_RGB565)
        if want_565:
            pf = struct.pack("!BBBBHHHBBB3x", 16, 16, 0, 1, 31, 63, 31, 11, 5, 0)
            bpp = 2
        else:
            pf = struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            bpp = 4
        sock.sendall(b"\x00\x00\x00\x00" + pf)
        # ClientDisplayConfiguration: 800x480 reference display.
        sock.sendall(ml.message(ml.EXT_CLIENT_DISPLAY_CONFIG, struct.pack(
            "!BBHHHHHHII", 1, 1, 0, 800, 480, 155, 93, 0,
            ml.PF_RGB565 if want_565 else ml.PF_ARGB888, 0)))
        sock.sendall(ml.message(ml.EXT_CLIENT_EVENT_CONFIG, struct.pack(
            "!HHHHIIIII", 0x6465, 0x4445, 0x6465, 0x4445, ml.KNOB0_REQUIRED,
            ml.DEVICE_KEY_BACKWARD, 0, 0, 1 | (1 << 8))))
        sock.sendall(struct.pack("!BBHHHH", 3, 0, 0, 0, width, height))
        fb = _read_framebuffer(sock, dump, width, height, bpp, want_565, session)
    finally:
        sock.close()
        dump.close()
    _save_png(fb, width, height, out / "phone-screen.png")
    print(f"  saved the phone's screen → {out}/phone-screen.png")


def _drain_ml_config(sock: socket.socket, dump, session: Session) -> int:
    """Read MirrorLink server-config messages until the phone stops sending them.
    Returns the phone's advertised pixel-format support (0 if none seen)."""
    pixel_formats = 0
    sock.settimeout(3.0)
    try:
        while True:
            head = _recv(sock, 1)
            if head[0] != ml.MSG_MIRRORLINK:
                # Not a MirrorLink message — push back by handling as framebuffer later
                # is complex; phones send config first, so treat this as end of config.
                sock.settimeout(15.0)
                _pushback(sock, head)
                break
            rest = _recv(sock, 3)
            ext, length = struct.unpack("!BH", rest)
            payload = _recv(sock, length)
            dump.write(head + rest + payload)
            name = ml.EXT_NAMES.get(ext, f"ext{ext}")
            session.event("vnc_ml_from_phone", ext=ext, name=name,
                          payload_hex=payload[:512].hex())
            if ext == ml.EXT_SERVER_DISPLAY_CONFIG and length >= 12:
                pixel_formats = struct.unpack_from("!I", payload, 8)[0]
    except TimeoutError:
        sock.settimeout(15.0)
    return pixel_formats


# A one-byte pushback buffer, since we peeked a non-MirrorLink message type.
_pushed: dict[int, bytes] = {}


def _pushback(sock: socket.socket, byte: bytes) -> None:
    _pushed[id(sock)] = byte


def _recv_typed(sock: socket.socket, dump) -> int:
    if id(sock) in _pushed:
        b = _pushed.pop(id(sock))
    else:
        b = _recv(sock, 1)
    dump.write(b)
    return b[0]


def _read_framebuffer(sock: socket.socket, dump, width: int, height: int, bpp: int,
                      rgb565: bool, session: Session) -> bytearray:
    fb = bytearray(width * height * 3)
    for _ in range(200):
        msg_type = _recv_typed(sock, dump)
        if msg_type == 1:  # SetColourMapEntries
            body = _recv(sock, 5)
            dump.write(body)
            _, n = struct.unpack("!xHH", body)
            dump.write(_recv(sock, 6 * n))
            continue
        if msg_type == ml.MSG_MIRRORLINK:
            rest = _recv(sock, 3)
            dump.write(rest)
            _, length = struct.unpack("!BH", rest)
            dump.write(_recv(sock, length))
            continue
        if msg_type != 0:
            raise ProbeError(f"unexpected VNC server message type {msg_type}")
        header = _recv(sock, 3)
        dump.write(header)
        (count,) = struct.unpack("!xH", header)
        got_data = False
        for _ in range(count):
            rh = _recv(sock, 12)
            dump.write(rh)
            x, y, w, h, enc = struct.unpack("!HHHHi", rh)
            if enc == ml.ENC_CONTEXT_INFO:
                dump.write(_recv(sock, 20))
                continue
            if enc == ml.ENC_DESKTOP_SIZE:
                continue
            if enc != 0:
                raise ProbeError(f"phone used unsupported encoding {enc}; raw dump saved")
            data = _recv(sock, w * h * bpp)
            dump.write(data)
            _blit(fb, width, x, y, w, h, data, bpp, rgb565)
            got_data = True
        if got_data:
            session.event("vnc_framebuffer", width=width, height=height)
            return fb
    return fb


def _blit(fb: bytearray, width: int, x: int, y: int, w: int, h: int, data: bytes,
          bpp: int, rgb565: bool) -> None:
    for row in range(h):
        for col in range(w):
            off = (row * w + col) * bpp
            if rgb565:
                (v,) = struct.unpack_from("<H", data, off)
                r, g, b = (v >> 11) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31
            else:
                b, g, r = data[off], data[off + 1], data[off + 2]
            o = ((y + row) * width + x + col) * 3
            fb[o:o + 3] = bytes((r, g, b))


def _save_png(fb: bytearray, width: int, height: int, path: Path) -> None:
    from ..http_descriptor import encode_png
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        rows += fb[y * width * 3:(y + 1) * width * 3]
    path.write_bytes(encode_png(width, height, bytes(rows)))


# ---------- orchestration ----------

def run(cfg: Config, *, device_spec: str = "", vendor: int | None = None,
        version: str = "1.1", interface: str = "", session_root: str = "") -> int:
    import os
    if os.geteuid() != 0:
        print("probe-phone must run as root (USB control transfer + network setup)",
              file=sys.stderr)
        return 1

    root = Path(session_root or cfg.session.root) / "probe-phone"
    directory = root / time.strftime("%Y-%m-%d_%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    session = Session(directory, boot_number=0)
    print(f"recording into {directory}")

    watcher: LinkWatcher | None = None
    try:
        if interface and (Path("/sys/class/net") / interface).exists():
            iface = interface
            print(f"using existing interface {iface} (skipping USB command)")
        else:
            if interface:
                # The phone leaves MirrorLink mode when the previous run ends, taking
                # its interface with it: wake it up again.
                print(f"{interface} is gone (phone left MirrorLink mode); "
                      "sending the USB command again")
            device = _resolve_device(device_spec, vendor)
            print(f"phone: {device.describe()}")
            before = {p.name for p in Path("/sys/class/net").glob("*")}
            result = usbhost.send_ml_command(device, version=version)
            session.event("ml_usb_command", **result)
            print(f"  MirrorLink USB command: {result['outcome']}")
            iface = wait_for_interface(before)
            print(f"  phone network interface: {iface}")

        watcher = LinkWatcher(iface, directory / "phone.pcap")
        watcher.start()
        _release_interface(iface)
        _bring_up(iface)
        mac = interface_mac(iface)
        ours, phone_ip = get_addresses(iface, mac, watcher, session)

        location = _discover(iface, phone_ip, session, our_ip=ours)
        host, port, desc = _fetch_descriptor(location, directory, session)
        print(f"  phone descriptor: {len(desc)} bytes → {directory}/device-description.xml")
        _report_versions(desc)

        _fetch_scpds(host, port, desc, directory)
        app_list = _client_handshake(host, port, desc, session, directory)
        _run_apps(host, port, app_list, session, directory)
    except (ProbeError, OSError, TimeoutError) as exc:
        session.event("probe_error", error=str(exc))
        print(f"FAILED: {exc}", file=sys.stderr)
        if watcher:
            watcher.stop()
            session.event("link_summary", summary=watcher.summary())
        session.close()
        _write_report(directory)
        return 1
    if watcher:
        watcher.stop()
        session.event("link_summary", summary=watcher.summary())
    session.close()
    _write_report(directory)
    print(f"\ndone. Summary: {directory}/report.txt")
    return 0


def _resolve_device(device_spec: str, vendor: int | None) -> usbhost.UsbDevice:
    if device_spec:
        bus, _, addr = device_spec.partition(":")
        for d in usbhost.list_devices():
            if d.bus == int(bus) and d.address == int(addr):
                return d
        raise ProbeError(f"no USB device at {device_spec}")
    device = usbhost.find_phone(vendor)
    if device is None:
        known = ", ".join(f"{v:#06x} {n}" for v, n in usbhost.KNOWN_VENDORS.items())
        raise ProbeError("no known MirrorLink phone found. Plug it in and enable "
                         "MirrorLink in its settings, or pass --device BUS:ADDR "
                         f"(mlpi probe-phone --list). Known vendors: {known}")
    return device


def _discover(iface: str, server: str, session: Session, our_ip: str = "") -> str:
    """M-SEARCH for the MirrorLink server; return its device-description LOCATION.

    Also listens for the phone's own NOTIFY announcements, in case it ignores M-SEARCH."""
    import select
    cfg = Config()
    msg = ssdp.render_msearch(cfg, search_target="urn:schemas-upnp-org:device:TmServerDevice:1")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, dhcp.SO_BINDTODEVICE, iface.encode())
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    socks = [sock]
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setsockopt(socket.SOL_SOCKET, dhcp.SO_BINDTODEVICE, iface.encode())
        listener.bind(("", 1900))
        listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton("239.255.255.250")
                            + socket.inet_aton(our_ip or "0.0.0.0"))
        socks.append(listener)
    except OSError as exc:   # port 1900 taken (e.g. minissdpd): M-SEARCH alone
        session.event("ssdp_listen_failed", error=str(exc))
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            for dest in (("239.255.255.250", 1900), (server, 1900)):
                try:
                    sock.sendto(msg, dest)
                except OSError as exc:
                    session.event("ssdp_send_failed", dest=dest[0], error=str(exc))
            end = time.monotonic() + 3
            while time.monotonic() < end:
                ready, _, _ = select.select(socks, [], [], max(0.0, end - time.monotonic()))
                for s in ready:
                    data, addr = s.recvfrom(65535)
                    text = data.decode("utf-8", "replace")
                    session.event("ssdp_rx", src=addr[0], text=text[:1000])
                    location = _header(text, "LOCATION")
                    if location and ("TmServerDevice" in text or addr[0] == server):
                        session.event("ssdp_response", src=addr[0], location=location)
                        return location
    finally:
        for s in socks:
            s.close()
    raise ProbeError("no SSDP response from the phone (no LOCATION header seen)")


def _header(text: str, name: str) -> str:
    for line in text.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def _fetch_descriptor(location: str, out: Path, session: Session) -> tuple[str, int, str]:
    m = re.match(r"http://([^:/]+)(?::(\d+))?(/.*)?", location)
    if not m:
        raise ProbeError(f"cannot parse LOCATION {location!r}")
    host, port, path = m.group(1), int(m.group(2) or 80), m.group(3) or "/"
    status, data = _http(host, port, "GET", path)
    if status != 200:
        raise ProbeError(f"descriptor → HTTP {status}")
    desc = data.decode("utf-8", "replace")
    (out / "device-description.xml").write_text(desc)
    session.event("device_description", location=location, bytes=len(data))
    return host, port, desc


def _report_versions(desc: str) -> None:
    ml_v = re.search(r"<majorVersion>(\d+)</majorVersion>\s*<minorVersion>(\d+)", desc)
    has_sig = "X_Signature" in desc
    manuf = _arg(desc, "manufacturer")
    model = _arg(desc, "modelName")
    print(f"  manufacturer={manuf!r} model={model!r} "
          f"MirrorLink={'.'.join(ml_v.groups()) if ml_v else 'unstated (=1.0)'} "
          f"X_Signature={'present' if has_sig else 'absent'}")


def _fetch_scpds(host: str, port: int, desc: str, out: Path) -> None:
    scpd_dir = out / "scpd"
    scpd_dir.mkdir(exist_ok=True)
    for url in sorted(set(re.findall(r"<SCPDURL>\s*([^<]+?)\s*</SCPDURL>", desc))):
        path = url if url.startswith("/") else "/" + url
        try:
            status, data = _http(host, port, "GET", path)
            if status == 200:
                (scpd_dir / (Path(path).name or "scpd.xml")).write_bytes(data)
        except OSError:
            continue


def _client_handshake(host: str, port: int, desc: str, session: Session, out: Path) -> str:
    prof_ctrl = _control_path(desc, _TM_PROF) or "/ctrl/TmClientProfile"
    app_ctrl = _control_path(desc, _TM_APP) or "/ctrl/TmApplicationServer"
    # Part 13 §7.3.3: SetClientProfile before any other action.
    escaped = (CLIENT_PROFILE.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    _soap(host, port, prof_ctrl, _TM_PROF, "SetClientProfile",
          f'<u:SetClientProfile xmlns:u="{_TM_PROF}"><ProfileID>0</ProfileID>'
          f'<ClientProfile>{escaped}</ClientProfile></u:SetClientProfile>')
    session.event("set_client_profile", control=prof_ctrl)
    listing = _arg(_soap(host, port, app_ctrl, _TM_APP, "GetApplicationList",
                         f'<u:GetApplicationList xmlns:u="{_TM_APP}">'
                         '<AppListingFilter>*</AppListingFilter><ProfileID>0</ProfileID>'
                         '</u:GetApplicationList>'), "AppListing")
    (out / "app-list.xml").write_text(listing)
    session.event("app_list", bytes=len(listing))
    apps = re.findall(r"<protocolID>\s*([^<]+?)\s*</protocolID>", listing)
    print(f"  phone advertises apps with protocols: {apps}")
    session._app_ctrl = app_ctrl  # type: ignore[attr-defined]
    return listing


def _run_apps(host: str, port: int, listing: str, session: Session, out: Path) -> None:
    app_ctrl = getattr(session, "_app_ctrl", "/ctrl/TmApplicationServer")
    for block in re.findall(r"<app>.*?</app>", listing, re.DOTALL):
        app_id = _arg(block, "appID")
        protocol = _arg(block, "protocolID")
        if not app_id:
            continue
        uri = _arg(_soap(host, port, app_ctrl, _TM_APP, "LaunchApplication",
                         f'<u:LaunchApplication xmlns:u="{_TM_APP}"><AppID>{app_id}</AppID>'
                         '<ProfileID>0</ProfileID></u:LaunchApplication>'), "AppURI")
        session.event("launch", app_id=app_id, protocol=protocol, uri=uri)
        print(f"  launched {app_id} ({protocol}) → {uri}")
        try:
            if protocol.upper() == "DAP":
                dap_dir = out / "dap"
                dap_dir.mkdir(exist_ok=True)
                run_dap(host, uri, session, dap_dir)
            elif protocol.upper() == "VNC" and uri.lower().startswith("vnc://"):
                vnc_screenshot(host, uri, session, out)
        except (ProbeError, OSError) as exc:
            session.event("app_error", app_id=app_id, protocol=protocol, error=str(exc))
            print(f"    {protocol} failed: {exc}")


def _write_report(directory: Path) -> None:
    from . import report
    try:
        (directory / "report.txt").write_text(report.summarise(directory) + "\n")
    except Exception:  # noqa: BLE001
        pass
