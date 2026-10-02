"""Pairing the phone from the car screen: a number pad for Android's pairing code.

Wireless debugging only accepts keys the phone has paired with. Pairing used to need
the laptop (`mlpi pair-phone`), and phones forget pairings (Android revokes unused
debugging authorisations after a while). This page lets the driver pair in the car:

  on the phone   Wireless debugging → Pair device with pairing code
  on the Pi      the phone announces its pairing port by mDNS (its screen is on while
                 the dialog is open); the car shows this page with the port filled in;
                 the driver types the 6-digit code and taps PAIR → `adb pair`.

If the announcement isn't seen, the port shown on the phone can be typed in too.
"""

from __future__ import annotations

import threading

from . import canvas as cv
from .launcher import BG, BUTTON, DIM, TEXT, Painter
from .video import VideoFrame

ACTIVE = "#ffd54f"
GOOD = "#43a047"
BAD = "#e53935"

# ("digit", "0".."9") | ("del", None) | ("pair", None) | ("field", "code"/"port")
# | ("later", None)
Target = tuple[str, object]


class PairingPage:
    CODE_LEN = 6
    PORT_LEN = 5

    def __init__(self, frame: VideoFrame) -> None:
        self.frame = frame
        self.width, self.height = frame.width, frame.height
        self.code = ""
        self.port = ""
        self.port_found = False             # filled in from the phone's announcement
        self.field = "code"
        self.message = ""
        self.message_colour = DIM
        self.busy = False
        self.targets: list[tuple[tuple[int, int, int, int], Target]] = []
        self._lock = threading.RLock()
        self.draw()

    # ----- state -----

    def reset(self) -> None:
        with self._lock:
            self.code, self.field, self.busy = "", "code", False
            if not self.port_found:
                self.port = ""
            self.set_message("")

    def set_port(self, port: int) -> None:
        """The phone announced its pairing port (it changes every time the dialog opens)."""
        with self._lock:
            if self.port_found and self.port == str(port):
                return
            self.port, self.port_found = str(port), True
            if self.field == "port":
                self.field = "code"
            self.draw()

    def forget_port(self) -> None:
        with self._lock:
            if self.port_found:
                self.port, self.port_found = "", False
                self.draw()

    def set_message(self, text: str, colour: str = DIM) -> None:
        with self._lock:
            self.message, self.message_colour = text, colour
            self.draw()

    def ready(self) -> bool:
        return len(self.code) == self.CODE_LEN and self.port.isdigit() and not self.busy

    def press(self, target: Target) -> str | None:
        """Apply a tap; returns "pair" when the PAIR button should start pairing,
        "later" when the page should go away."""
        kind, value = target
        with self._lock:
            if kind == "digit":
                if self.field == "code" and len(self.code) < self.CODE_LEN:
                    self.code += value
                    if len(self.code) == self.CODE_LEN and not self.port:
                        self.field = "port"
                elif self.field == "port" and len(self.port) < self.PORT_LEN:
                    self.port += value
                    self.port_found = False
            elif kind == "del":
                if self.field == "code":
                    self.code = self.code[:-1]
                else:
                    self.port = self.port[:-1]
                    self.port_found = False
            elif kind == "field":
                self.field = value
            elif kind == "pair":
                if self.ready():
                    return "pair"
                self.message = ("TYPE ALL 6 DIGITS OF THE CODE" if len(self.code) < 6
                                else "TYPE THE PORT SHOWN ON THE PHONE")
                self.message_colour = BAD
            elif kind == "later":
                return "later"
            self.draw()
        return None

    # ----- drawing -----

    def draw(self) -> None:
        with self._lock:
            buf = bytearray(self.frame.frame_bytes)
            p = Painter(buf, self.width, self.height)
            p.rect(0, 0, self.width, self.height, BG)
            self.targets = []
            p.text(20, 16, "PAIR WITH YOUR PHONE", 3, TEXT)
            lines = ("1. PHONE: DEVELOPER OPTIONS > WIRELESS DEBUGGING",
                     "2. TAP: PAIR DEVICE WITH PAIRING CODE",
                     "3. TYPE THE 6-DIGIT CODE BELOW, THEN PAIR")
            for i, line in enumerate(lines):
                p.text(20, 52 + i * 22, line, 2, DIM)
            self._field(p, (20, 140), "CODE", self.code, self.CODE_LEN, "code")
            port_label = "PORT (FOUND)" if self.port_found else "PORT"
            self._field(p, (20, 250), port_label, self.port, self.PORT_LEN, "port")
            if self.message:
                p.text(20, 372, self.message[:40], 2, self.message_colour)
            later = (20, 410, 170, 54)
            p.rounded(*later, 10, BUTTON)
            p.text_centered(later[0] + later[2] // 2, later[1] + 16, "LATER", 3, TEXT)
            self.targets.append((later, ("later", None)))
            self._keypad(p)
            self.frame.update(bytes(buf))

    def _field(self, p: Painter, at, label: str, value: str, length: int, name: str) -> None:
        x, y = at
        active = self.field == name
        p.text(x, y, label, 2, ACTIVE if active else DIM)
        box_w, gap = 54, 8
        for i in range(length):
            bx = x + i * (box_w + gap)
            p.rounded(bx, y + 22, box_w, 64, 8, ACTIVE if active and i == len(value) else BUTTON)
            p.rect(bx + 4, y + 26, box_w - 8, 56, BG if active and i == len(value) else BUTTON)
            if i < len(value):
                p.text_centered(bx + box_w // 2, y + 36, value[i], 5, TEXT)
        self.targets.append(((x, y, length * (box_w + gap), 90), ("field", name)))

    def _keypad(self, p: Painter) -> None:
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "DEL", "0", "PAIR"]
        w, h, gap = 82, 72, 10
        x0 = self.width - 20 - 3 * w - 2 * gap
        y0 = 140
        for i, key in enumerate(keys):
            box = (x0 + (i % 3) * (w + gap), y0 + (i // 3) * (h + gap), w, h)
            colour = BUTTON
            if key == "PAIR":
                colour = GOOD if self.ready() else BUTTON
            p.rounded(*box, 12, colour)
            scale = 4 if key.isdigit() else 3
            p.text_centered(box[0] + w // 2, box[1] + (h - cv.GLYPH_H * scale) // 2, key,
                            scale, TEXT)
            target: Target = (("digit", key) if key.isdigit() else
                              ("del", None) if key == "DEL" else ("pair", None))
            self.targets.append((box, target))

    def target_at(self, x: int, y: int) -> Target | None:
        with self._lock:
            targets = list(self.targets)
        for (tx, ty, tw, th), target in targets:
            if tx <= x < tx + tw and ty <= y < ty + th:
                return target
        return None
