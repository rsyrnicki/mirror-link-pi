"""SSDP discovery CLI.

Successor to Robert's ``py-lsupnp.py`` (kept under ``legacy/``). Difference: binds to a
specific interface (so on a multi-NIC laptop you discover usb0-stand-in devices, not
your home WiFi), and uses our own ``ssdp.render_msearch`` for protocol consistency.
"""

from __future__ import annotations

import socket
import sys

from .. import netinfo, ssdp
from ..config import Config

_RECV_BUF = 65507


def run(cfg: Config, *, timeout: float = 4.0, verbose: bool = False) -> int:
    iface = cfg.network.interface
    try:
        address = cfg.network.address or netinfo.interface_address(iface)
    except netinfo.InterfaceNotFoundError as exc:
        print(f"discover: {exc}", file=sys.stderr)
        return 1

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind((address, 0))
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address)
    )
    sock.settimeout(timeout)

    payload = ssdp.render_msearch(cfg, search_target="ssdp:all", mx=int(timeout))
    sock.sendto(payload, (cfg.ssdp.multicast_group, cfg.ssdp.multicast_port))
    if verbose:
        print(f"discover: M-SEARCH sent from {address} (iface {iface}, timeout {timeout}s)")

    seen: set[str] = set()
    try:
        while True:
            try:
                data, addr = sock.recvfrom(_RECV_BUF)
            except TimeoutError:
                break
            host = addr[0]
            seen.add(host)
            if verbose:
                print(f"--- reply from {host}:{addr[1]} ---")
                print(data.decode("utf-8", errors="replace").rstrip())
    finally:
        sock.close()

    if not seen:
        print("discover: no responses")
        return 0
    for host in sorted(seen):
        print(host)
    return 0
