"""Minimal DHCP server for the single client on the USB link (the car).

Replaces dnsmasq so the Pi needs no extra packages (the SD card is prepared on the
laptop and the Pi never has internet). It only has to serve one client on a
point-to-point link, which keeps it small:

  DISCOVER → OFFER,  REQUEST → ACK (or NAK for a foreign address),  INFORM → ACK

Every packet in both directions is decoded and written to the session log — the
car's DHCP options (hostname, vendor class, parameter list) are a useful fingerprint.

Replies are unicast to the offered address when the client did not set the
broadcast flag (what dnsmasq did in the captured sessions): we add a static
neighbour entry first, since the client cannot answer ARP before it has an address.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .session import STAGE_DHCP, Session

log = logging.getLogger(__name__)

MAGIC_COOKIE = b"\x63\x82\x53\x63"
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)

DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
MSG_NAMES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE", 5: "ACK", 6: "NAK",
             7: "RELEASE", 8: "INFORM"}

OPT_SUBNET, OPT_ROUTER, OPT_DNS, OPT_HOSTNAME = 1, 3, 6, 12
OPT_BROADCAST = 28
OPT_REQUESTED_IP, OPT_LEASE, OPT_MSG_TYPE, OPT_SERVER_ID = 50, 51, 53, 54
OPT_PARAM_LIST, OPT_VENDOR_CLASS, OPT_CLIENT_ID, OPT_END = 55, 60, 61, 255


@dataclass
class DhcpPacket:
    op: int
    xid: int
    secs: int
    flags: int
    ciaddr: str
    yiaddr: str
    siaddr: str
    giaddr: str
    chaddr: bytes
    options: dict[int, bytes] = field(default_factory=dict)

    @property
    def msg_type(self) -> int | None:
        value = self.options.get(OPT_MSG_TYPE)
        return value[0] if value else None

    @property
    def mac(self) -> str:
        return ":".join(f"{b:02x}" for b in self.chaddr[:6])

    @property
    def wants_broadcast(self) -> bool:
        return bool(self.flags & 0x8000)


def parse_packet(data: bytes) -> DhcpPacket:
    if len(data) < 240 or data[236:240] != MAGIC_COOKIE:
        raise ValueError("not a DHCP packet")
    op, _htype, hlen, _hops, xid, secs, flags = struct.unpack("!BBBBIHH", data[:12])
    ciaddr, yiaddr, siaddr, giaddr = (socket.inet_ntoa(data[i:i + 4]) for i in (12, 16, 20, 24))
    chaddr = data[28:28 + min(hlen, 16)]
    options: dict[int, bytes] = {}
    i = 240
    while i < len(data):
        code = data[i]
        if code == 0:
            i += 1
            continue
        if code == OPT_END or i + 1 >= len(data):
            break
        length = data[i + 1]
        options[code] = options.get(code, b"") + data[i + 2:i + 2 + length]
        i += 2 + length
    return DhcpPacket(op, xid, secs, flags, ciaddr, yiaddr, siaddr, giaddr, chaddr, options)


def build_reply(req: DhcpPacket, msg_type: int, *, yiaddr: str, server_ip: str,
                options: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        2, 1, 6, 0, req.xid, 0, req.flags,
        socket.inet_aton(req.ciaddr if msg_type == ACK and req.msg_type == INFORM else "0.0.0.0"),
        socket.inet_aton(yiaddr),
        socket.inet_aton(server_ip),
        socket.inet_aton(req.giaddr),
        req.chaddr.ljust(16, b"\0"),
        b"", b"",
    )
    opts = bytearray(MAGIC_COOKIE)
    opts += bytes([OPT_MSG_TYPE, 1, msg_type])
    opts += bytes([OPT_SERVER_ID, 4]) + socket.inet_aton(server_ip)
    for code, value in options:
        opts += bytes([code, len(value)]) + value
    opts.append(OPT_END)
    packet = header + bytes(opts)
    return packet.ljust(300, b"\0")  # BOOTP minimum size; some clients insist


def describe_options(options: dict[int, bytes]) -> dict[str, object]:
    """Human-readable view of the interesting options, for the event log."""
    out: dict[str, object] = {}
    for code, value in sorted(options.items()):
        if code == OPT_MSG_TYPE:
            out["msg_type"] = MSG_NAMES.get(value[0], value[0])
        elif code in (OPT_HOSTNAME, OPT_VENDOR_CLASS):
            out["hostname" if code == OPT_HOSTNAME else "vendor_class"] = value.decode(
                "latin-1")
        elif code == OPT_PARAM_LIST:
            out["param_request_list"] = list(value)
        elif code in (OPT_REQUESTED_IP, OPT_SERVER_ID) and len(value) == 4:
            out["requested_ip" if code == OPT_REQUESTED_IP else "server_id"] = socket.inet_ntoa(
                value)
        else:
            out[f"opt{code}"] = value.hex()
    return out


class DhcpServer:
    def __init__(self, *, interface: str, server_ip: str, prefix: int, client_ip: str,
                 lease_seconds: int = 3600, offer_router: bool = True, offer_dns: bool = True,
                 session: Session | None = None, is_car: bool = True) -> None:
        self.interface = interface
        self.is_car = is_car            # False: the phone's Wi-Fi lease, not a car stage
        self.server_ip = server_ip
        self.network = ipaddress.ip_network(f"{server_ip}/{prefix}", strict=False)
        self.client_ip = client_ip
        self.lease_seconds = lease_seconds
        self.offer_router = offer_router
        self.offer_dns = offer_dns
        self.session = session
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._leases: dict[str, str] = {}   # mac → ip

    # ----- lifecycle -----

    def serve_forever(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, self.interface.encode())
        self._sock.bind(("", 67))
        self._sock.settimeout(1.0)
        log.info("DHCP server on %s: %s → client %s", self.interface, self.server_ip,
                 self.client_ip)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError as exc:
                log.warning("DHCP recv failed: %s", exc)
                self._stop.wait(1.0)
                continue
            try:
                self.handle(data, addr)
            except Exception:  # noqa: BLE001 - never let one bad packet kill DHCP
                log.exception("DHCP packet handling failed: %r", data[:64])
        self._sock.close()

    def stop(self) -> None:
        self._stop.set()

    # ----- protocol -----

    def leased_addresses(self) -> list[str]:
        return list(self._leases.values())

    def connected_addresses(self, macs: list[str] | None) -> list[str]:
        """Leases of the given client MACs (those associated right now), newest lease
        first. A phone that forgot and re-joined the network comes back with a new
        random MAC and a new address; the old lease is dead. None = MACs unknown:
        every lease, newest first."""
        leases = list(self._leases.items())[::-1]
        if macs is None:
            return [ip for _mac, ip in leases]
        wanted = {m.lower() for m in macs}
        return [ip for mac, ip in leases if mac.lower() in wanted]

    def lease_for(self, mac: str) -> str:
        ip = self._leases.get(mac)
        if ip is None:
            base = ipaddress.ip_address(self.client_ip)
            used = set(self._leases.values())
            candidate = base
            while str(candidate) in used or str(candidate) == self.server_ip:
                candidate += 1
            if candidate not in self.network:
                raise RuntimeError("DHCP pool exhausted")
            ip = str(candidate)
            self._leases[mac] = ip
        return ip

    def reply_for(self, req: DhcpPacket) -> tuple[int, str, bytes] | None:
        """Decide the reply. Returns (msg_type, yiaddr, packet) or None to stay silent."""
        mtype = req.msg_type
        if req.op != 1 or mtype is None:
            return None
        if mtype == DISCOVER:
            ip = self.lease_for(req.mac)
            return OFFER, ip, self._build(req, OFFER, ip)
        if mtype == REQUEST:
            ip = self.lease_for(req.mac)
            requested = req.options.get(OPT_REQUESTED_IP)
            wanted = socket.inet_ntoa(requested) if requested and len(requested) == 4 else (
                req.ciaddr if req.ciaddr != "0.0.0.0" else ip)
            server_id = req.options.get(OPT_SERVER_ID)
            if server_id and socket.inet_ntoa(server_id) != self.server_ip:
                return None  # the client picked another server
            if wanted != ip:
                return NAK, "0.0.0.0", self._build(req, NAK, "0.0.0.0", with_lease=False)
            return ACK, ip, self._build(req, ACK, ip)
        if mtype == INFORM:
            return ACK, "0.0.0.0", self._build(req, ACK, "0.0.0.0", with_lease=False)
        return None  # DECLINE / RELEASE: nothing to send

    def _build(self, req: DhcpPacket, mtype: int, yiaddr: str, *, with_lease: bool = True
               ) -> bytes:
        opts: list[tuple[int, bytes]] = []
        if mtype != NAK:
            opts.append((OPT_SUBNET, socket.inet_aton(str(self.network.netmask))))
            opts.append((OPT_BROADCAST, socket.inet_aton(str(self.network.broadcast_address))))
            if self.offer_router:
                opts.append((OPT_ROUTER, socket.inet_aton(self.server_ip)))
            if self.offer_dns:
                opts.append((OPT_DNS, socket.inet_aton(self.server_ip)))
            if with_lease:
                opts.append((OPT_LEASE, struct.pack("!I", self.lease_seconds)))
        return build_reply(req, mtype, yiaddr=yiaddr, server_ip=self.server_ip, options=opts)

    def handle(self, data: bytes, addr) -> None:
        try:
            req = parse_packet(data)
        except ValueError:
            return
        info = describe_options(req.options)
        log.info("DHCP %s from %s xid=%08x %s", MSG_NAMES.get(req.msg_type, req.msg_type),
                 req.mac, req.xid, info)
        if self.session:
            self.session.event("dhcp_rx", mac=req.mac, xid=f"{req.xid:08x}", src=addr[0],
                               flags=req.flags, ciaddr=req.ciaddr, options=info)
        decision = self.reply_for(req)
        if decision is None:
            return
        mtype, yiaddr, packet = decision
        dest = self._destination(req, mtype, yiaddr)
        assert self._sock is not None
        self._sock.sendto(packet, (dest, 68))
        log.info("DHCP %s → %s (%s) yiaddr=%s", MSG_NAMES[mtype], req.mac, dest, yiaddr)
        if self.session:
            self.session.event("dhcp_tx", mac=req.mac, xid=f"{req.xid:08x}",
                               msg_type=MSG_NAMES[mtype], yiaddr=yiaddr, dest=dest)
            if mtype == ACK and yiaddr != "0.0.0.0":
                if self.is_car:
                    self.session.reach(STAGE_DHCP, client=yiaddr, mac=req.mac)
                    self.session.note("car address", f"{yiaddr} ({req.mac})")
                else:
                    self.session.note("phone address", f"{yiaddr} ({req.mac})")

    def _destination(self, req: DhcpPacket, mtype: int, yiaddr: str) -> str:
        if req.ciaddr != "0.0.0.0":
            return req.ciaddr
        if mtype == NAK or req.wants_broadcast or yiaddr == "0.0.0.0":
            return "255.255.255.255"
        try:
            subprocess.run(
                ["ip", "neigh", "replace", yiaddr, "lladdr", req.mac, "dev", self.interface,
                 "nud", "reachable"],
                check=True, capture_output=True, timeout=5,
            )
            return yiaddr
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not add neighbour entry (%s); broadcasting instead", exc)
            return "255.255.255.255"


# ---------- client (used by probe-phone, where the phone is the DHCP server) ----------

def _client_packet(msg_type: int, xid: int, mac: bytes, *, requested: str | None = None,
                   server_id: str | None = None, ciaddr: str = "0.0.0.0") -> bytes:
    header = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0, 0x8000,
                         socket.inet_aton(ciaddr), b"\0" * 4, b"\0" * 4, b"\0" * 4,
                         mac.ljust(16, b"\0"), b"", b"")
    opts = bytearray(MAGIC_COOKIE)
    opts += bytes([OPT_MSG_TYPE, 1, msg_type])
    if requested:
        opts += bytes([OPT_REQUESTED_IP, 4]) + socket.inet_aton(requested)
    if server_id:
        opts += bytes([OPT_SERVER_ID, 4]) + socket.inet_aton(server_id)
    opts += bytes([OPT_HOSTNAME, 4]) + b"mlpi"
    opts += bytes([OPT_PARAM_LIST, 3, OPT_SUBNET, OPT_ROUTER, OPT_DNS])
    opts.append(OPT_END)
    return (header + bytes(opts)).ljust(300, b"\0")


def dhcp_client(interface: str, mac: bytes, *, timeout: float = 20.0,
                on_event=lambda kind, **f: None) -> dict[str, str]:
    """Obtain a lease on ``interface`` (the phone is the server). Returns
    {"address", "netmask", "server", "router"}; raises TimeoutError on no reply."""
    import random

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode())
    sock.bind(("", 68))
    sock.settimeout(2.0)
    xid = random.getrandbits(32)
    deadline = time.monotonic() + timeout
    try:
        sock.sendto(_client_packet(DISCOVER, xid, mac), ("255.255.255.255", 67))
        on_event("dhcp_tx", msg_type="DISCOVER", xid=f"{xid:08x}")
        offer = None
        while time.monotonic() < deadline and offer is None:
            try:
                data, _ = sock.recvfrom(4096)
            except TimeoutError:
                sock.sendto(_client_packet(DISCOVER, xid, mac), ("255.255.255.255", 67))
                continue
            pkt = parse_packet(data)
            if pkt.xid == xid and pkt.msg_type == OFFER:
                offer = pkt
        if offer is None:
            raise TimeoutError("no DHCP OFFER from the phone")
        server = socket.inet_ntoa(offer.options[OPT_SERVER_ID])
        on_event("dhcp_rx", msg_type="OFFER", yiaddr=offer.yiaddr, server=server)

        sock.sendto(_client_packet(REQUEST, xid, mac, requested=offer.yiaddr,
                                   server_id=server), ("255.255.255.255", 67))
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except TimeoutError:
                continue
            pkt = parse_packet(data)
            if pkt.xid != xid:
                continue
            if pkt.msg_type == ACK:
                netmask = (socket.inet_ntoa(pkt.options[OPT_SUBNET])
                           if OPT_SUBNET in pkt.options else "255.255.255.0")
                router = (socket.inet_ntoa(pkt.options[OPT_ROUTER])
                          if OPT_ROUTER in pkt.options else "")
                lease = {"address": pkt.yiaddr, "netmask": netmask, "server": server,
                         "router": router}
                on_event("dhcp_rx", msg_type="ACK", **lease)
                # Configure the interface (ip is present on Pi OS / Fedora).
                prefix = sum(bin(int(o)).count("1") for o in netmask.split("."))
                subprocess.run(["ip", "addr", "replace", f"{pkt.yiaddr}/{prefix}",
                                "dev", interface], check=False)
                subprocess.run(["ip", "link", "set", interface, "up"], check=False)
                return lease
            if pkt.msg_type == NAK:
                raise TimeoutError("phone sent DHCPNAK")
        raise TimeoutError("no DHCP ACK from the phone")
    finally:
        sock.close()
