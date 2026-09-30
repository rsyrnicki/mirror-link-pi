"""Pi health in the session log: undervoltage, throttling, temperature, load, memory.

The car powers the Pi from a USB port. When decoding phone video pushes the Zero 2 W's
CPU to full load, a weak port can let the voltage sag: the firmware then throttles the
CPU (sluggish video) or the Pi browns out and reboots (a session that just stops). The
firmware's ``get_throttled`` flags say which, so they're recorded whenever they change
and once a minute otherwise.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from .session import Session

log = logging.getLogger(__name__)

THROTTLE_BITS = {
    0: "undervoltage now", 1: "arm frequency capped now", 2: "throttled now",
    3: "soft temperature limit now",
    16: "undervoltage has occurred", 17: "frequency capping has occurred",
    18: "throttling has occurred", 19: "soft temperature limit has occurred",
}


def decode_throttled(value: int) -> list[str]:
    return [name for bit, name in THROTTLE_BITS.items() if value >> bit & 1]


def read_throttled() -> int | None:
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                             timeout=5).stdout            # "throttled=0x50005"
        return int(out.strip().split("=")[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def snapshot() -> dict:
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            mem[key] = int(value.split()[0]) // 1024
    throttled = read_throttled()
    return {
        "throttled": f"0x{throttled:x}" if throttled is not None else None,
        "throttled_flags": decode_throttled(throttled) if throttled else [],
        "temp_c": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "load": [round(x, 2) for x in os.getloadavg()],
        "mem_available_mb": mem.get("MemAvailable"),
        "mem_total_mb": mem.get("MemTotal"),
    }


class HealthMonitor:
    def __init__(self, session: Session, *, interval: float = 10.0, every: int = 6) -> None:
        self.session = session
        self.interval = interval
        self.every = every
        self._stop = threading.Event()

    def run(self) -> None:
        last_throttled = object()
        n = 0
        while not self._stop.is_set():
            snap = snapshot()
            changed = snap["throttled"] != last_throttled
            if changed or n % self.every == 0:
                self.session.event("health", **snap)
                if changed and snap["throttled_flags"]:
                    log.warning("Pi power/thermal: %s", ", ".join(snap["throttled_flags"]))
                    self.session.note("POWER/THERMAL", ", ".join(snap["throttled_flags"]))
                last_throttled = snap["throttled"]
            n += 1
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
