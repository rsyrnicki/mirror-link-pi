"""Pretend to be a car head unit for testing the SSDP responder.

Successor to Robert's ``simulate_upnp.py`` (kept under ``legacy/``). Sends a unicast
M-SEARCH at the target and prints the reply, so you can test the responder without
actually generating multicast traffic.
"""

from __future__ import annotations

import socket
import sys

from .. import ssdp
from ..config import Config


def run(cfg: Config, *, target: str, port: int = 1900, timeout: float = 3.0) -> int:
    payload = ssdp.render_msearch(cfg, search_target="upnp:rootdevice", mx=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    print(f"simulate-car: sending M-SEARCH to {target}:{port}")
    sock.sendto(payload, (target, port))
    try:
        data, addr = sock.recvfrom(65507)
    except TimeoutError:
        print("simulate-car: no reply within timeout", file=sys.stderr)
        return 1
    finally:
        sock.close()
    print(f"--- reply from {addr[0]}:{addr[1]} ---")
    print(data.decode("utf-8", errors="replace").rstrip())
    return 0
