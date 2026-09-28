"""Blink the Pi's green ACT LED to show the furthest stage reached this boot.

There is no screen and no network access to the Pi in the car, so this is the only
live feedback:

  1 short blink every 3 s   running, the car has not enumerated the USB gadget yet
  1 blink  + pause          USB link up
  2 blinks + pause          DHCP: the car took an address
  3 blinks + pause          UPnP: the car fetched our device descriptor
  4 blinks + pause          the car called LaunchApplication
  solid on                  the car connected to the VNC server

The stage is sticky: it shows the best result of this boot even after the car gives up.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .session import STAGE_VNC_CONNECT

log = logging.getLogger(__name__)

_CANDIDATES = ("/sys/class/leds/ACT", "/sys/class/leds/led0")


def find_led(path: str = "") -> Path | None:
    for candidate in ([path] if path else _CANDIDATES):
        p = Path(candidate)
        if (p / "brightness").exists():
            return p
    return None


class StatusLed:
    def __init__(self, path: str = "") -> None:
        self.led = find_led(path)
        self.stage = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._original_trigger: str | None = None
        self._on_value = "1"
        if self.led is None:
            log.info("no status LED found; LED feedback disabled")
            return
        try:
            triggers = (self.led / "trigger").read_text()
            current = [t for t in triggers.split() if t.startswith("[")]
            self._original_trigger = current[0].strip("[]") if current else None
            (self.led / "trigger").write_text("none")
            max_b = (self.led / "max_brightness").read_text().strip()
            self._on_value = max_b if max_b.isdigit() and int(max_b) > 0 else "1"
        except OSError as exc:
            log.warning("cannot take over LED %s: %s", self.led, exc)
            self.led = None

    def set_stage(self, stage: int) -> None:
        self.stage = stage
        self._wake.set()

    def _set(self, on: bool) -> None:
        if self.led is None:
            return
        try:
            (self.led / "brightness").write_text(self._on_value if on else "0")
        except OSError:
            pass

    def _sleep(self, seconds: float) -> bool:
        """Sleep, but return early (True) on a stage change or stop."""
        woke = self._wake.wait(seconds)
        self._wake.clear()
        return woke or self._stop.is_set()

    def run(self) -> None:
        if self.led is None:
            return
        while not self._stop.is_set():
            stage = self.stage
            if stage >= STAGE_VNC_CONNECT:
                self._set(True)
                self._sleep(3.0)
            elif stage == 0:
                self._set(True)
                if self._sleep(0.08):
                    continue
                self._set(False)
                self._sleep(2.9)
            else:
                interrupted = False
                for _ in range(stage):
                    self._set(True)
                    if self._sleep(0.2):
                        interrupted = True
                        break
                    self._set(False)
                    if self._sleep(0.3):
                        interrupted = True
                        break
                if not interrupted:
                    self._set(False)
                    self._sleep(1.5)
        self._restore()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _restore(self) -> None:
        if self.led is None:
            return
        try:
            if self._original_trigger:
                (self.led / "trigger").write_text(self._original_trigger)
        except OSError:
            pass
