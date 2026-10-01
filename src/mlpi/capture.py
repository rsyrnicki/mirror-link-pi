"""Record every Ethernet frame on usb0 into a pcap file (no tcpdump needed).

Runs as its own service (``mlpi-capture.service``) so that a crash of the main
server never costs us the packet trace, and vice versa. Uses an AF_PACKET raw socket
(both directions) and writes classic pcap (linktype Ethernet), readable by
Wireshark/tshark/tcpdump. Survives the interface disappearing and coming back, and
fsyncs every few seconds because the car cuts power without warning.

The screen content itself is left out: a kernel filter drops the large VNC packets the
Pi sends (pixel data), so the trace keeps every handshake, request and input event but
the streaming phone video no longer costs CPU time and hundreds of MB of SD writes. The
files are also capped in total (``max_total``); after that the capture stops.
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import struct
import time
from pathlib import Path

log = logging.getLogger(__name__)

ETH_P_ALL = 0x0003
SO_ATTACH_FILTER = 26
PCAP_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)


def pixel_filter(vnc_port: int = 5900, keep_up_to: int = 256) -> list[tuple[int, int, int, int]]:
    """Classic BPF: drop IPv4 TCP frames from ``vnc_port`` longer than ``keep_up_to``."""
    accept, drop = (0x06, 0, 0, 262144), (0x06, 0, 0, 0)
    return [
        (0x28, 0, 0, 12),           # 0  ldh [12]           ethertype
        (0x15, 0, 9, 0x0800),       # 1  != IPv4 → accept
        (0x30, 0, 0, 23),           # 2  ldb [23]           IP protocol
        (0x15, 0, 7, 6),            # 3  != TCP → accept
        (0x28, 0, 0, 20),           # 4  ldh [20]           fragment offset
        (0x45, 5, 0, 0x1FFF),       # 5  fragment → accept
        (0xB1, 0, 0, 14),           # 6  ldxb 4*([14]&0xf)  IP header length
        (0x48, 0, 0, 14),           # 7  ldh [x+14]         TCP source port
        (0x15, 0, 2, vnc_port),     # 8  != VNC → accept
        (0x80, 0, 0, 0),            # 9  ld len
        (0x25, 1, 0, keep_up_to),   # 10 > keep_up_to → drop
        accept,                     # 11
        drop,                       # 12
    ]


def attach_filter(sock: socket.socket, program: list[tuple[int, int, int, int]]) -> None:
    code = b"".join(struct.pack("HBBI", *ins) for ins in program)
    buf = ctypes.create_string_buffer(code)
    fprog = struct.pack("HL", len(program), ctypes.addressof(buf))
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, fprog)


class PcapWriter:
    def __init__(self, path: Path, *, max_bytes: int = 64 * 1024 * 1024,
                 max_total: int | None = None) -> None:
        self.base = path
        self.max_bytes = max_bytes
        self.max_total = max_total      # None = rotate forever
        self.total = 0
        self.full = False
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
        if self.full or self._fh is None:
            return
        self._fh.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)) + frame)
        self._written += 16 + len(frame)
        self.total += 16 + len(frame)
        self.packets += 1
        if self.max_total is not None and self.total >= self.max_total:
            log.warning("capture reached %d MB; not recording any more this boot",
                        self.total >> 20)
            self.full = True
            self.close()
            return
        if self._written >= self.max_bytes:
            self._fh.close()
            self.part += 1
            self._open()
        self.maybe_sync()

    def maybe_sync(self, every: float = 3.0) -> None:
        now = time.monotonic()
        if now - self._last_sync >= every and self._fh is not None:
            try:
                self._fh.flush()
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


def capture(interface: str, out: Path, *, stop=None, vnc_port: int = 5900,
            max_total: int = 100 * 1024 * 1024) -> int:
    """Capture until ``stop`` (a threading.Event) is set, ``max_total`` bytes are
    written, or forever."""
    writer = PcapWriter(out, max_total=max_total)
    sock = None
    try:
        while stop is None or not stop.is_set():
            if writer.full:             # idle rather than exit: a restart would start over
                if sock:
                    sock.close()
                    sock = None
                if stop is None:
                    time.sleep(60)
                else:
                    stop.wait(60)
                continue
            if sock is None:
                try:
                    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                         socket.htons(ETH_P_ALL))
                    try:
                        attach_filter(sock, pixel_filter(vnc_port))
                    except OSError as exc:
                        log.warning("no kernel filter (%s); capturing everything", exc)
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
