"""The phone's state for the launcher's status bar: clock, battery, signal, Do Not Disturb.

One ``adb shell`` call every poll (``POLL_COMMAND``) prints a few sections separated by
``===``; ``parse_poll`` turns them into a ``PhoneState``. The clock is the phone's (the Pi
has no real-time clock): the phone's local time is stored as an offset to the Pi's
monotonic clock, so it keeps ticking between polls.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

POLL_COMMAND = (
    "dumpsys battery; echo ===; getprop gsm.network.type; echo ===; "
    "settings get global zen_mode; echo ===; date +%s,%z; echo ===; "
    "dumpsys telephony.registry | grep mSignalStrength=; true"
)
# (`; true`: without a SIM, grep finds nothing and the shell's exit status would be 1.)

# getprop gsm.network.type → what the phone's status bar would show
_NETWORK_NAMES = (("NR", "5G"), ("LTE", "4G"), ("HSPA", "3G"), ("HSDPA", "3G"),
                  ("HSUPA", "3G"), ("UMTS", "3G"), ("TD_SCDMA", "3G"), ("EDGE", "E"),
                  ("GPRS", "G"), ("GSM", "G"))


@dataclass
class PhoneState:
    battery: int | None = None          # percent
    charging: bool = False
    signal: int | None = None           # 0..4 bars, None = no SIM / unknown
    network: str = ""                   # "5G", "4G", …
    dnd: bool | None = None             # Do Not Disturb on?
    screen_on: bool = False             # the phone's own screen
    clock_base: float | None = None     # phone local time (s) − time.monotonic()
    extra: dict = field(default_factory=dict)

    def clock(self, now: float | None = None) -> str:
        """The phone's local time as HH:MM, '' before the first poll."""
        if self.clock_base is None:
            return ""
        t = int((time.monotonic() if now is None else now) + self.clock_base)
        return f"{t // 3600 % 24:02d}:{t // 60 % 60:02d}"


def network_name(prop: str) -> str:
    """``gsm.network.type`` (comma-separated per SIM) → '5G', '4G', '3G', 'E', 'G' or ''."""
    for value in (v.strip().upper() for v in prop.split(",")):
        for key, name in _NETWORK_NAMES:
            if key in value:
                return name
    return ""


def parse_battery(text: str) -> tuple[int | None, bool]:
    level = re.search(r"^\s*level:\s*(\d+)", text, re.M)
    status = re.search(r"^\s*status:\s*(\d+)", text, re.M)
    powered = re.findall(r"^\s*(?:AC|USB|Wireless|Dock) powered:\s*true", text, re.M)
    charging = bool(status and status.group(1) == "2") or bool(powered)
    return (min(100, int(level.group(1))) if level else None), charging


def parse_signal(text: str) -> int | None:
    """Best ``level=N`` (0..4) in the mSignalStrength lines of telephony.registry."""
    levels = [int(n) for n in re.findall(r"\blevel=(\d)", text)]
    return max(levels) if levels else None


def parse_clock(text: str, now: float | None = None) -> float | None:
    """``date +%s,%z`` (e.g. ``1790000000,+0200``) → phone local time − monotonic."""
    m = re.match(r"\s*(\d+),([+-])(\d\d)(\d\d)", text)
    if not m:
        return None
    offset = (int(m.group(3)) * 3600 + int(m.group(4)) * 60) * (1 if m.group(2) == "+" else -1)
    return int(m.group(1)) + offset - (time.monotonic() if now is None else now)


def parse_poll(text: str, now: float | None = None) -> dict:
    """Output of ``POLL_COMMAND`` → fields for ``PhoneState`` (missing ones left out)."""
    parts = (text.split("===") + [""] * 5)[:5]
    battery, charging = parse_battery(parts[0])
    out: dict = {"charging": charging}
    if battery is not None:
        out["battery"] = battery
    out["network"] = network_name(parts[1])
    zen = parts[2].strip()
    if zen.isdigit():
        out["dnd"] = zen != "0"
    base = parse_clock(parts[3], now)
    if base is not None:
        out["clock_base"] = base
    out["signal"] = parse_signal(parts[4])
    return out
