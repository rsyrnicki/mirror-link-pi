"""SSDP advertiser + M-SEARCH responder.

Consolidates Robert's ``service.py``, ``service_first.py``, ``service_second.py`` and
``mirrorlink_server.py`` from ``legacy/`` into one config-driven implementation.

A single UDP socket is used for both sending and receiving so that the kernel does not
pick a different source IP for the response than for the alive announcement (this used
to bite multi-NIC setups in Robert's code).

Bug fixes carried over from the legacy code:
  - ``loging.debug`` typo (silently swallowed by outer except in service.py)
  - missing ``import time`` despite calling ``time.sleep``
  - missing ``SO_REUSEPORT`` (matters when running discover.py on the same host)
  - ``IP_ADD_MEMBERSHIP`` keyed to the configured interface IP, not INADDR_ANY,
    so SSDP from the laptop's WiFi doesn't bleed into our usb0-stand-in tests.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from .config import Config

log = logging.getLogger(__name__)


def _usn(cfg: Config, nt: str) -> str:
    uuid = f"uuid:{cfg.ssdp.device_uuid}"
    return uuid if nt == uuid else f"{uuid}::{nt}"


def render_alive(cfg: Config, location: str, nt: str = "upnp:rootdevice") -> bytes:
    """Render an SSDP NOTIFY ssdp:alive datagram for the configured device."""
    return _render_message(
        "NOTIFY * HTTP/1.1",
        host=f"{cfg.ssdp.multicast_group}:{cfg.ssdp.multicast_port}",
        nt=nt,
        nts="ssdp:alive",
        usn=_usn(cfg, nt),
        location=location,
        cache_control=f"max-age={cfg.ssdp.max_age_seconds}",
        server="Linux/UPnP/1.0 mlpi/0.2",
    )


def render_byebye(cfg: Config, nt: str = "upnp:rootdevice") -> bytes:
    """Render an SSDP NOTIFY ssdp:byebye datagram (sent on shutdown)."""
    return _render_message(
        "NOTIFY * HTTP/1.1",
        host=f"{cfg.ssdp.multicast_group}:{cfg.ssdp.multicast_port}",
        nt=nt,
        nts="ssdp:byebye",
        usn=_usn(cfg, nt),
    )


def advertised_targets(cfg: Config) -> list[str]:
    """Every NT a UPnP device announces: rootdevice, its uuid, device + service types."""
    return ["upnp:rootdevice", f"uuid:{cfg.ssdp.device_uuid}", *_RESPOND_TO_ALL_TARGETS[1:]]


def render_msearch_response(cfg: Config, location: str, st: str = "upnp:rootdevice") -> bytes:
    """Render the HTTP/1.1 200 reply to an M-SEARCH request.

    Per UPnP 1.0 §1.3.3, the response ST must mirror the request ST exactly, and the
    USN format depends on what the ST was — direct uuid match, rootdevice, or one of
    the device/service URNs.
    """
    if st == f"uuid:{cfg.ssdp.device_uuid}":
        usn = st
    else:
        usn = f"uuid:{cfg.ssdp.device_uuid}::{st}"
    return _render_message(
        "HTTP/1.1 200 OK",
        cache_control=f"max-age={cfg.ssdp.max_age_seconds}",
        ext="",
        location=location,
        server="Linux/UPnP/1.0 mlpi/0.2",
        st=st,
        usn=usn,
    )


# All search-targets we will respond to when the client sends ST=ssdp:all.
# Per UPnP, we must send one HTTP/1.1 200 per matching target.
# These are the actual MirrorLink services per ETSI TS 103 544-12 Table 2 — earlier
# versions of this list contained TmServerStateMachineProfile and TmProfileService
# which do NOT exist in the spec.
_RESPOND_TO_ALL_TARGETS = (
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:TmServerDevice:1",
    "urn:schemas-upnp-org:service:TmApplicationServer:1",
    "urn:schemas-upnp-org:service:TmClientProfile:1",
    "urn:schemas-upnp-org:service:TmNotificationServer:1",
)


def _parse_msearch_header(text: str, name: str) -> str | None:
    """Return the value of the named M-SEARCH header (case-insensitive)."""
    needle = name.lower() + ":"
    for line in text.split("\r\n"):
        if line.lower().startswith(needle):
            return line.split(":", 1)[1].strip()
    return None


def render_msearch(cfg: Config, search_target: str = "ssdp:all", mx: int = 3) -> bytes:
    """Render an M-SEARCH datagram for client-side discovery."""
    return _render_message(
        "M-SEARCH * HTTP/1.1",
        host=f"{cfg.ssdp.multicast_group}:{cfg.ssdp.multicast_port}",
        man='"ssdp:discover"',
        mx=str(mx),
        st=search_target,
    )


def _render_message(start_line: str, **headers: str) -> bytes:
    field_lookup = {
        "cache_control": "CACHE-CONTROL",
    }
    lines = [start_line]
    for key, value in headers.items():
        name = field_lookup.get(key, key.upper())
        lines.append(f"{name}: {value}" if value else f"{name}:")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode("utf-8")


class SsdpResponder:
    """Periodic SSDP advertiser + M-SEARCH responder.

    Bind, multicast join and send all happen on a single socket bound to the configured
    interface. ``serve_forever`` blocks; call ``stop`` from another thread to break out.
    """

    def __init__(self, cfg: Config, address: str, location: str, session=None) -> None:
        self.cfg = cfg
        self.address = address          # interface IPv4 to bind to
        self.location = location        # absolute URL of root device descriptor
        self.session = session          # optional mlpi.session.Session
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._last_alive = 0.0
        self._announce = threading.Event()

    def serve_forever(self) -> None:
        self._sock = self._open_socket()
        log.info("SSDP responder bound to %s, advertising LOCATION=%s", self.address, self.location)
        self._send_alive()
        try:
            while not self._stop.is_set():
                self._tick()
        finally:
            self._send_byebye()
            self._sock.close()
            self._sock = None
            log.info("SSDP responder stopped")

    def stop(self) -> None:
        self._stop.set()

    def announce(self) -> None:
        """Ask for an immediate ssdp:alive burst (e.g. when the USB link comes up)."""
        self._announce.set()

    def _tick(self) -> None:
        assert self._sock is not None
        # Wake up regularly so we can re-emit alive and check the stop flag.
        self._sock.settimeout(0.5)
        try:
            data, addr = self._sock.recvfrom(65507)
        except TimeoutError:
            data = b""
            addr = None
        except OSError as exc:
            # Transient while the USB link bounces; don't let the thread die.
            log.debug("SSDP recv failed: %s", exc)
            time.sleep(0.5)
            data, addr = b"", None
        if data:
            self._handle_request(data, addr)
        if self._announce.is_set():
            self._announce.clear()
            for _ in range(3):   # UDP is lossy; UPnP recommends repeating
                self._send_alive()
        elif time.monotonic() - self._last_alive >= self.cfg.ssdp.notify_interval_seconds:
            self._send_alive()

    def _handle_request(self, data: bytes, addr) -> None:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        if not text.startswith("M-SEARCH"):
            # NOTIFY messages from other devices on the segment land here too; ignore.
            return
        st = _parse_msearch_header(text, "ST") or "upnp:rootdevice"
        log.debug("M-SEARCH from %s for ST=%s:\n%s", addr, st, text)
        if self.session is not None:
            self.session.event("ssdp_msearch", src=f"{addr[0]}:{addr[1]}", st=st, raw=text)
        assert self._sock is not None
        # ssdp:all → respond once per known target.
        # Specific ST → respond exactly with that ST mirrored, but only if it matches
        # something we actually advertise (per UPnP §1.3.3 we may stay silent otherwise).
        if st == "ssdp:all":
            targets = _RESPOND_TO_ALL_TARGETS
        elif st == f"uuid:{self.cfg.ssdp.device_uuid}":
            targets = (st,)
        elif st in _RESPOND_TO_ALL_TARGETS:
            targets = (st,)
        else:
            log.debug("M-SEARCH from %s: ST %r doesn't match anything we offer", addr, st)
            return
        for target in targets:
            reply = render_msearch_response(self.cfg, self.location, st=target)
            self._sock.sendto(reply, addr)
            log.debug("Replied to M-SEARCH from %s with ST=%s", addr, target)

    def _send_alive(self) -> None:
        assert self._sock is not None
        dest = (self.cfg.ssdp.multicast_group, self.cfg.ssdp.multicast_port)
        self._last_alive = time.monotonic()
        try:
            for nt in advertised_targets(self.cfg):
                self._sock.sendto(render_alive(self.cfg, self.location, nt), dest)
            log.debug("Sent ssdp:alive")
        except OSError as exc:
            # No carrier yet (car not connected) is the common case; stay quiet-ish.
            log.debug("Failed to send ssdp:alive: %s", exc)

    def _send_byebye(self) -> None:
        if self._sock is None:
            return
        dest = (self.cfg.ssdp.multicast_group, self.cfg.ssdp.multicast_port)
        try:
            for nt in advertised_targets(self.cfg):
                self._sock.sendto(render_byebye(self.cfg, nt), dest)
            log.debug("Sent ssdp:byebye")
        except OSError as exc:
            log.warning("Failed to send ssdp:byebye: %s", exc)

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT lets discover.py share port 1900 on the same host.
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", self.cfg.ssdp.multicast_port))
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.cfg.ssdp.multicast_ttl
        )
        # Bind multicast send + group membership to the configured interface so that on a
        # multi-NIC host SSDP doesn't leak via the WiFi default route.
        if_addr = socket.inet_aton(self.address)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, if_addr)
        mreq = struct.pack("=4s4s", socket.inet_aton(self.cfg.ssdp.multicast_group), if_addr)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        return sock
