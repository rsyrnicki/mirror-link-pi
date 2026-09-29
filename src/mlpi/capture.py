"""Record every Ethernet frame on usb0 into a pcap file (no tcpdump needed).

Runs as its own service (``mlpi-capture.service``) so that a crash of the main
server never costs us the packet trace, and vice versa. Uses an AF_PACKET raw socket
(both directions) and writes classic pcap (linktype Ethernet), readable by
Wireshark/tshark/tcpdump. Survives the interface disappearing and coming back, and
fsyncs every few seconds because the car cuts power without warning.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import time
from pathlib import Path

log = logging.getLogger(__name__)

ETH_P_ALL = 0x0003
PCAP_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)


class PcapWriter:
    def __init__(self, path: Path, *, max_bytes: int = 200 * 1024 * 1024) -> None:
        self.base = path
        self.max_bytes = max_bytes
        self.part = 0
        self._fh = None
        self._written = 0
        self._last_sync = time.monotonic()
        self.packets = 0
        self._open()

    def _open(self) -> None:
        path = self.base if self.part == 0 else self.base.with_name(
            f"{self.base.stem}.{self.part}{self.base.suffix}")
        self._fh = open(path, "ab")
        if self._fh.tell() == 0:
            self._fh.write(PCAP_HEADER)
        self._written = self._fh.tell()
        log.info("capturing into %s", path)

    def write(self, frame: bytes, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        assert self._fh is not None
        self._fh.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)) + frame)
        self._written += 16 + len(frame)
        self.packets += 1
        self._fh.flush()
        if self._written >= self.max_bytes:
            self._fh.close()
            self.part += 1
            self._open()
        self.maybe_sync()

    def maybe_sync(self, every: float = 3.0) -> None:
        now = time.monotonic()
        if now - self._last_sync >= every and self._fh is not None:
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._last_sync = now

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None


def capture(interface: str, out: Path, *, stop=None) -> int:
    """Capture until ``stop`` (a threading.Event) is set, or forever."""
    writer = PcapWriter(out)
    sock = None
    try:
        while stop is None or not stop.is_set():
            if sock is None:
                try:
                    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                         socket.htons(ETH_P_ALL))
                    sock.bind((interface, 0))
                    sock.settimeout(1.0)
                    log.info("capture socket bound to %s", interface)
                except OSError as exc:
                    log.info("waiting for %s: %s", interface, exc)
                    if sock:
                        sock.close()
                    sock = None
                    time.sleep(1.0)
                    continue
            try:
                frame = sock.recv(262144)
            except TimeoutError:
                writer.maybe_sync()
                continue
            except OSError as exc:
                # ENETDOWN / ENXIO when the gadget is re-created: rebind.
                log.warning("capture recv failed (%s); rebinding", exc)
                sock.close()
                sock = None
                time.sleep(0.5)
                continue
            writer.write(frame)
    finally:
        if sock:
            sock.close()
        writer.close()
    return writer.packets
