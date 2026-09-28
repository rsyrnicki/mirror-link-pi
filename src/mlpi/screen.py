"""The "app" shown on the head unit: a status screen drawn into a Canvas.

Designed to be unmistakable when it appears in the car, and to prove the round trip:
  * big title + boot number, so a photo of the car screen identifies the session
  * a moving block and a seconds counter: the picture is live, not a cached frame
  * colour bars with labels: verifies the pixel format the car negotiated
  * touch/knob/key input echoed on screen: proves input flows car → Pi
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from . import canvas as cv
from .session import STAGE_NAMES, Session


class StatusScreen:
    def __init__(self, canvas: cv.Canvas, *, session: Session | None = None,
                 variant_name: Callable[[], str] = lambda: "-") -> None:
        self.canvas = canvas
        self.session = session
        self.variant_name = variant_name
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self.vnc_client = "-"
        self.pixel_format = "-"
        self.frames_sent = 0
        self.last_touch = "-"
        self.last_key = "-"
        self._touches: list[tuple[int, int]] = []
        self._stop = threading.Event()
        self._tick = 0
        s = self._layout()
        self._line_ys = s["lines"]
        self.draw_static()

    # ----- layout -----

    def _layout(self) -> dict:
        w, h = self.canvas.width, self.canvas.height
        self.scale_title = max(2, min(6, w // 160))
        self.scale_text = max(1, min(3, w // 260))   # 800 px wide → 3 (18 px per char)
        header_h = cv.GLYPH_H * self.scale_title + 4 * self.scale_title
        line_h = (cv.GLYPH_H + 4) * self.scale_text
        self.bars_h = max(24, h // 8)
        self.ticker_y = h - self.bars_h - 3 * self.scale_text - 10
        first = header_h + 2 * line_h
        n_lines = max(1, min(7, (self.ticker_y - first) // line_h))
        self.header_h = header_h
        self.line_h = line_h
        return {"lines": [first + i * line_h for i in range(n_lines)]}

    def draw_static(self) -> None:
        c = self.canvas
        w, h = c.width, c.height
        with c.lock:
            c.fill_rect(0, 0, w, h, cv.BG)
            c.fill_rect(0, 0, w, self.header_h, cv.HEADER)
            title = "MIRRORLINK-PI"
            tw = cv.text_width(title, self.scale_title)
            c.text((w - tw) // 2, 2 * self.scale_title, title, scale=self.scale_title,
                   colour=cv.WHITE)
            sub = "HELLO CAR - THIS PICTURE COMES FROM THE PI"
            sw = cv.text_width(sub, self.scale_text)
            c.text(max(4, (w - sw) // 2), self.header_h + self.line_h // 2, sub,
                   scale=self.scale_text, colour=cv.YELLOW)
            self._draw_bars()
        c.commit()

    def _draw_bars(self) -> None:
        c = self.canvas
        w, h = c.width, c.height
        bars = [(cv.RED, "R"), (cv.GREEN, "G"), (cv.BLUE, "B"), (cv.YELLOW, "Y"),
                (cv.CYAN, "C"), (cv.MAGENTA, "M"), (cv.WHITE, "W"), (cv.BLACK, "K")]
        bw = w // len(bars)
        y = h - self.bars_h
        for i, (colour, label) in enumerate(bars):
            x = i * bw
            c.fill_rect(x, y, bw if i < len(bars) - 1 else w - x, self.bars_h, colour)
            label_colour = cv.BLACK if colour in (cv.WHITE, cv.YELLOW, cv.CYAN, cv.GREEN) \
                else cv.WHITE
            c.text(x + 4, y + 4, label, scale=self.scale_text, colour=label_colour)

    # ----- dynamic part -----

    def status_lines(self) -> list[str]:
        up = int(time.monotonic() - self._t0)
        boot = self.session.boot_number if self.session else 0
        stage = self.session.stage if self.session else 0
        with self._lock:
            return [
                f"BOOT #{boot}   UP {up // 3600:02d}:{up // 60 % 60:02d}:{up % 60:02d}",
                f"STAGE {stage}: {STAGE_NAMES.get(stage, '?')}",
                f"VARIANT: {self.variant_name()}",
                f"VNC: {self.vnc_client}  FRAMES {self.frames_sent}",
                f"FORMAT: {self.pixel_format}",
                f"TOUCH: {self.last_touch}",
                f"KEY: {self.last_key}",
            ]

    def refresh(self) -> None:
        c = self.canvas
        w = c.width
        max_chars = max(1, (w - 8) // (cv.ADVANCE * self.scale_text))
        with c.lock:
            for y, line in zip(self._line_ys, self.status_lines(), strict=False):
                c.fill_rect(0, y - self.scale_text, w, self.line_h, cv.BG)
                c.text(8, y, line[:max_chars], scale=self.scale_text, colour=cv.WHITE)
            self._draw_ticker()
            with self._lock:
                touches = list(self._touches)
            arm = 6 * self.scale_text
            for tx, ty in touches:   # crosshair, big enough to spot on a car screen
                c.fill_rect(tx - arm, ty - 1, 2 * arm + 1, 3, cv.ORANGE)
                c.fill_rect(tx - 1, ty - arm, 3, 2 * arm + 1, cv.ORANGE)
        c.commit()

    def _draw_ticker(self) -> None:
        c = self.canvas
        w = c.width
        size = 3 * self.scale_text + 4
        y = self.ticker_y
        c.fill_rect(0, y, w, size, cv.BLACK)
        steps = 20
        pos = self._tick % steps
        c.fill_rect(4 + pos * (w - 8 - size) // (steps - 1), y, size, size, cv.OK)

    # ----- input from the VNC client -----

    def on_pointer(self, x: int, y: int, buttons: int) -> None:
        with self._lock:
            self.last_touch = f"{x},{y} BUTTONS {buttons}"
            if buttons:
                self._touches.append((x, y))
                del self._touches[:-50]

    def on_key(self, keysym: int, down: bool) -> None:
        with self._lock:
            self.last_key = f"0X{keysym:04X} {'DOWN' if down else 'UP'}"

    def on_client(self, peer: str, pixel_format: str) -> None:
        with self._lock:
            self.vnc_client = peer
            self.pixel_format = pixel_format

    def on_frame(self) -> None:
        with self._lock:
            self.frames_sent += 1

    # ----- lifecycle -----

    def run(self) -> None:
        while not self._stop.is_set():
            self._tick += 1
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - drawing must never take the server down
                import logging
                logging.getLogger(__name__).exception("screen refresh failed")
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()
