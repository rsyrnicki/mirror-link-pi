"""The phone's Bluetooth address, for the Bluetooth audio entries (``bt_apps`` variants).

The car asks for our device description and app list as soon as it sees the Pi, usually
before the phone has joined the Pi's Wi-Fi. So the address is read from the phone over
adb once it connects and remembered on the card (``<session root>/phone-bt-address``);
``[phone] bt_address`` in mlpi.toml overrides it.
"""

from __future__ import annotations

import re
from pathlib import Path

STATE_FILE = "phone-bt-address"
_MAC = re.compile(r"\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")


def normalise(text: str) -> str:
    """'AA:BB:CC:DD:EE:FF' / 'aabbccddeeff' → 'AABBCCDDEEFF'; '' if not an address."""
    hexdigits = re.sub(r"[^0-9A-Fa-f]", "", text or "")
    if len(hexdigits) != 12 or hexdigits in ("000000000000", "020000000000"):
        return ""
    return hexdigits.upper()


def current(cfg) -> str:
    configured = normalise(getattr(cfg.phone, "bt_address", ""))
    if configured:
        return configured
    try:
        return normalise((Path(cfg.session.root) / STATE_FILE).read_text())
    except OSError:
        return ""


def remember(root: str | Path, address: str) -> bool:
    """Store a newly learned address; True if it changed."""
    address = normalise(address)
    if not address:
        return False
    path = Path(root) / STATE_FILE
    try:
        if path.read_text().strip() == address:
            return False
    except OSError:
        pass
    path.write_text(address + "\n")
    return True


def parse_phone_output(text: str) -> str:
    """Output of `settings get secure bluetooth_address` (just the address) or of
    `dumpsys bluetooth_manager`, whose "Bluetooth Status" section starts with the
    adapter's own "address: …" line. Paired devices (the car!) are listed further down
    with their addresses too, so only that first labelled line counts.
    """
    stripped = text.strip()
    if _MAC.fullmatch(stripped):
        return normalise(stripped)
    m = re.search(r"^\s*address:\s*(\S+)", text, re.M | re.I)
    return normalise(m.group(1)) if m else ""
