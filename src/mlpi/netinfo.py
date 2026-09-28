"""Network introspection without external dependencies.

Replaces ``netifaces`` (unmaintained, breaks on Python 3.13+) with ``ip -j addr``.
Requires iproute2, which is default on both Raspberry Pi OS Bookworm and Fedora.
"""

from __future__ import annotations

import json
import subprocess


class InterfaceNotFoundError(LookupError):
    """The given interface does not exist or has no IPv4 address assigned."""


def interface_address(interface: str, *, runner=None) -> str:
    """Return the first IPv4 address assigned to ``interface``.

    ``runner`` is a hook for tests; defaults to :func:`subprocess.run`.
    """
    runner = runner or _run_ip
    output = runner(["ip", "-j", "addr", "show", "dev", interface])
    data = json.loads(output)
    if not data:
        raise InterfaceNotFoundError(f"interface {interface!r} not present")
    addr_info = data[0].get("addr_info", [])
    for entry in addr_info:
        if entry.get("family") == "inet":
            return entry["local"]
    raise InterfaceNotFoundError(f"interface {interface!r} has no IPv4 address")


def list_interfaces(*, runner=None) -> list[str]:
    """Return all interface names with an IPv4 address (excludes loopback)."""
    runner = runner or _run_ip
    output = runner(["ip", "-j", "addr", "show"])
    data = json.loads(output)
    out: list[str] = []
    for iface in data:
        name = iface.get("ifname")
        if not name or name == "lo":
            continue
        if any(a.get("family") == "inet" for a in iface.get("addr_info", [])):
            out.append(name)
    return out


def _run_ip(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("`ip` command not found — install iproute2") from exc
    except subprocess.CalledProcessError as exc:
        raise InterfaceNotFoundError(exc.stderr.strip() or str(exc)) from exc
    return proc.stdout
