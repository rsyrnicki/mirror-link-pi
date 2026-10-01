"""The Pi's own launcher for phone mode, drawn for the car's 800×480 screen.

Phone launchers can't be used on scrcpy's virtual display: Android only puts a
"secondary home" there (on Samsung, One UI's stripped-down DeX launcher with tiny
icons), and ordinary launchers look squashed at 800×480. So the Pi draws the home
screen itself:

  home page      status bar (phone clock, signal, battery, media keys, Do Not Disturb,
                 phone screen) above large tiles for the favourite apps + "All apps"
  all-apps pages every launchable app on the phone (from scrcpy's app list), paged
  Home button    drawn over a corner of the phone's video; brings the tiles back

Everything is drawn straight into RGB565 frames (``VideoFrame``) with the same 5×7
pixel font as the status screen — no image libraries on the Pi. The font is ASCII
upper case only, so app names are transliterated (Ä → AE, é → E, …).
"""

from __future__ import annotations

import re
import threading
import unicodedata
import zlib
from dataclasses import dataclass

from . import canvas as cv
from .phonestatus import PhoneState
from .video import VideoFrame


def rgb565(colour: str | tuple[int, int, int]) -> bytes:
    if isinstance(colour, str):
        h = colour.lstrip("#")
        colour = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    r, g, b = colour
    return ((r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)).to_bytes(2, "little")


_TRANSLIT = {"Ä": "AE", "Ö": "OE", "Ü": "UE", "ß": "SS", "&": "+", "'": "'", "’": "'"}


def display_name(name: str) -> str:
    """App name in what the 5×7 font can draw."""
    out = []
    for ch in name.upper():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isspace():
            out.append(" ")
        else:
            base = unicodedata.normalize("NFKD", ch)[:1]   # É → E, Ç → C, …
            if base in cv.GLYPHS:
                out.append(base)                             # anything else is dropped
    return re.sub(r"\s+", " ", "".join(out)).strip() or "?"


def wrap(text: str, max_chars: int, max_lines: int = 2) -> list[str]:
    """Word-wrap ``text``; overlong words are cut, the last line gets '…'-style '.'."""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        while len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max_chars - 1] + "."
    return lines


class Painter:
    """Minimal drawing on an RGB565 little-endian buffer."""

    def __init__(self, buf: bytearray, width: int, height: int) -> None:
        self.buf, self.width, self.height = buf, width, height

    def rect(self, x: int, y: int, w: int, h: int, colour) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        run = rgb565(colour) * (x1 - x0)
        stride = self.width * 2
        for row in range(y0, y1):
            o = row * stride
            self.buf[o + 2 * x0:o + 2 * x1] = run

    def rounded(self, x: int, y: int, w: int, h: int, r: int, colour) -> None:
        """Rectangle with (stepped) rounded corners."""
        r = max(0, min(r, w // 2, h // 2))
        self.rect(x + r, y, w - 2 * r, h, colour)
        for i in range(r):
            inset = r - int((r * r - (r - i - 0.5) ** 2) ** 0.5)
            self.rect(x + inset, y + i, r - inset, 1, colour)
            self.rect(x + inset, y + h - 1 - i, r - inset, 1, colour)
            self.rect(x + w - r, y + i, r - inset, 1, colour)
            self.rect(x + w - r, y + h - 1 - i, r - inset, 1, colour)
        self.rect(x, y + r, r, h - 2 * r, colour)
        self.rect(x + w - r, y + r, r, h - 2 * r, colour)

    def text(self, x: int, y: int, text: str, scale: int, colour) -> int:
        cx = x
        for ch in text.upper():
            for gy, gx, length in cv.GLYPHS.get(ch, cv.GLYPHS["?"]):
                self.rect(cx + gx * scale, y + gy * scale, length * scale, scale, colour)
            cx += cv.ADVANCE * scale
        return cx - x

    def text_centered(self, cx: int, y: int, text: str, scale: int, colour) -> None:
        self.text(cx - cv.text_width(text, scale) // 2, y, text, scale, colour)


@dataclass(frozen=True)
class App:
    name: str
    package: str
    colour: str = ""

    @property
    def tile_colour(self) -> str:
        if self.colour:
            return self.colour
        return _AUTO_COLOURS[zlib.crc32(self.package.encode()) % len(_AUTO_COLOURS)]


_AUTO_COLOURS = ["#5c6bc0", "#26a69a", "#ef6c00", "#8e24aa", "#00897b", "#c0392b",
                 "#3949ab", "#6d4c41", "#546e7a", "#7cb342", "#d81b60", "#0277bd"]

DEFAULT_APPS = (
    App("Google Maps", "com.google.android.apps.maps", "#1a73e8"),
    App("HERE WeGo", "com.here.app.maps", "#00a8a5"),
    App("Spotify", "com.spotify.music", "#1db954"),
    App("Audible", "com.audible.application", "#e8830c"),
    App("Home Assistant", "io.homeassistant.companion.android", "#1a9ad9"),
    App("WhatsApp", "com.whatsapp", "#1faa55"),
    App("Phone", "com.samsung.android.dialer", "#34a853"),
)

BG = "#101418"
TEXT = "#f2f2f2"
DIM = "#8a939e"
BUTTON = "#2a323c"


def parse_apps(entries) -> list[App]:
    """``[phone] apps`` from mlpi.toml: [{name=…, package=…, colour=…}, …]."""
    apps = []
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("package"):
            apps.append(App(str(entry.get("name") or entry["package"]), str(entry["package"]),
                            str(entry.get("colour") or entry.get("color") or "")))
    return apps or list(DEFAULT_APPS)


def parse_app_list(text: str) -> list[App]:
    """Parse the output of the scrcpy server's ``list_apps=true``.

    Lines look like `` * Name<padding> package`` (``*`` system, ``-`` user app); a
    name longer than 30 characters is followed by a line holding only the package.
    """
    apps: list[App] = []
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^ [*-] (.*?)(?:\s{2,}(\S+))?$", line)
        if m:
            name, package = m.group(1).strip(), m.group(2)
            if package:
                apps.append(App(name, package))
                pending = None
            else:
                pending = name
        elif pending is not None and line.strip() and " " not in line.strip():
            apps.append(App(pending, line.strip()))
            pending = None
    seen, unique = set(), []
    for app in sorted(apps, key=lambda a: display_name(a.name)):
        if app.package not in seen:
            seen.add(app.package)
            unique.append(app)
    return unique


# ("app", App) | ("all", None) | ("home", None) | ("prev", None) | ("next", None)
# | ("media", "previous" / "play_pause" / "next") | ("dnd", None) | ("screen", None)
Target = tuple[str, object]

ACTIVE_DND = "#7b1fa2"
ACTIVE_SCREEN = "#1565c0"
OFF = "#3a414b"
GREEN, AMBER, RED = "#43a047", "#f9a825", "#e53935"


class Launcher:
    """Home page, all-apps pages, and the Home button overlaid on the phone's video."""

    HOME_W, HOME_H = 44, 64  # px, the Home button overlaid on the phone's video
    HOME_MARGIN = 4
    HOME_POSITIONS = ("right", "left", "top-left", "top-right", "bottom-left",
                      "bottom-right", "off")
    TOP = 64                # header height

    def __init__(self, frame: VideoFrame, favourites: list[App], *,
                 home_button: str = "right") -> None:
        if home_button not in self.HOME_POSITIONS:
            raise ValueError(f"home_button must be one of {self.HOME_POSITIONS}")
        self.home_position = home_button
        self.frame = frame
        self.favourites = favourites
        self.visible_favourites = list(favourites)   # narrowed once the app list is known
        self.all_apps: list[App] = []
        self.width, self.height = frame.width, frame.height
        self.page = -1                     # -1 = home page, 0.. = all-apps pages
        self.status = ""
        self.state = PhoneState()
        self.targets: list[tuple[tuple[int, int, int, int], Target]] = []
        self._lock = threading.RLock()      # drawn from the input and the polling thread
        self._clock_shown = ""
        self.draw()

    # ----- pages -----

    PER_PAGE_COLS, PER_PAGE_ROWS = 4, 3

    @property
    def per_page(self) -> int:
        return self.PER_PAGE_COLS * self.PER_PAGE_ROWS

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.all_apps) // self.per_page))

    def set_all_apps(self, apps: list[App]) -> None:
        colours = {a.package: a.colour for a in self.favourites if a.colour}
        self.all_apps = [App(a.name, a.package, colours.get(a.package, a.colour)) for a in apps]
        if apps:            # hide favourites that aren't installed on this phone
            installed = {a.package for a in apps}
            self.visible_favourites = [f for f in self.favourites if f.package in installed]
        self.page = min(self.page, self.pages - 1)
        self.draw()

    def go(self, page: int) -> None:
        self.page = max(-1, min(page, self.pages - 1))
        self.draw()

    def set_state(self, **fields) -> None:
        """Update the status bar (redrawn only if something visible changed)."""
        with self._lock:
            changed = False
            for key, value in fields.items():
                if getattr(self.state, key) != value:
                    setattr(self.state, key, value)
                    changed = True
            if changed or self.state.clock() != self._clock_shown:
                self.draw()

    def tick(self) -> None:
        """Called every few seconds: redraw when the clock's minute changes."""
        with self._lock:
            if self.page < 0 and self.state.clock() != self._clock_shown:
                self.draw()

    # ----- drawing -----

    def _grid(self, n: int, cols: int, rows: int, top: int) -> list[tuple[int, int, int, int]]:
        margin, gap = 16, 14
        tw = (self.width - 2 * margin - (cols - 1) * gap) // cols
        th = (self.height - top - margin - (rows - 1) * gap) // rows
        return [(margin + (i % cols) * (tw + gap), top + (i // cols) * (th + gap), tw, th)
                for i in range(n)]

    def _tile(self, p: Painter, box, name: str, colour: str, big: bool) -> None:
        x, y, w, h = box
        p.rounded(x, y, w, h, 16, colour)
        label = display_name(name)
        # Big font unless a word would have to be cut; then the smaller one.
        for scale in (3, 2):
            max_chars = max(1, (w - 12) // (cv.ADVANCE * scale))
            if all(len(word) <= max_chars for word in label.split(" ")) and \
                    len(wrap(label, max_chars, max_lines=99)) <= 2:
                break
        lines = wrap(label, max_chars)
        if big:
            letter = max(4, min(9, (h - 70) // 9))
            p.text_centered(x + w // 2, y + 14, label[:1], letter, TEXT)
        line_h = (cv.GLYPH_H + 3) * scale
        y0 = y + h - 12 - line_h * len(lines) if big else y + (h - line_h * len(lines)) // 2
        for i, line in enumerate(lines):
            p.text_centered(x + w // 2, y0 + i * line_h, line, scale, TEXT)

    def _button(self, p: Painter, box, label: str) -> None:
        x, y, w, h = box
        p.rounded(x, y, w, h, 10, BUTTON)
        p.text_centered(x + w // 2, y + (h - cv.GLYPH_H * 3) // 2, label, 3, TEXT)

    def draw(self) -> None:
        with self._lock:
            self._draw()

    def _draw(self) -> None:
        buf = bytearray(self.frame.frame_bytes)
        p = Painter(buf, self.width, self.height)
        p.rect(0, 0, self.width, self.height, BG)
        self.targets = []
        if self.page < 0:
            self._status_bar(p)
            items = [(app.name, app.tile_colour, ("app", app))
                     for app in self.visible_favourites[:7]]
            items.append(("All apps", "#3a3f47", ("all", None)))
            cols = 4 if len(items) > 6 else 3
            rows = -(-len(items) // cols)
            for box, (name, colour, target) in zip(self._grid(len(items), cols, rows, self.TOP),
                                                   items, strict=False):
                self._tile(p, box, name, colour, big=True)
                self.targets.append((box, target))
        else:
            back = (16, 10, 150, 44)
            self._button(p, back, "< HOME")
            self.targets.append((back, ("home", None)))
            title = f"ALL APPS {self.page + 1}/{self.pages}"
            p.text_centered(self.width // 2, 22, title, 3, TEXT)
            if self.page > 0:
                prev = (self.width - 200, 10, 84, 44)
                self._button(p, prev, "<")
                self.targets.append((prev, ("prev", None)))
            if self.page < self.pages - 1:
                nxt = (self.width - 100, 10, 84, 44)
                self._button(p, nxt, ">")
                self.targets.append((nxt, ("next", None)))
            apps = self.all_apps[self.page * self.per_page:(self.page + 1) * self.per_page]
            if not apps:
                p.text_centered(self.width // 2, self.height // 2, "LOADING APP LIST ...", 3, DIM)
            for box, app in zip(self._grid(len(apps), self.PER_PAGE_COLS, self.PER_PAGE_ROWS,
                                           self.TOP + 4), apps, strict=False):
                self._tile(p, box, app.name, app.tile_colour, big=False)
                self.targets.append((box, ("app", app)))
        self.frame.update(bytes(buf))

    @property
    def home_rect(self) -> tuple[int, int, int, int] | None:
        """(x, y, w, h) of the Home button on the video, None when switched off.
        "left"/"right" = the middle of that edge, where apps rarely put controls."""
        pos = self.home_position
        if pos == "off":
            return None
        w, h, m = self.HOME_W, self.HOME_H, self.HOME_MARGIN
        x = m if "left" in pos else self.width - w - m
        if pos in ("left", "right"):
            y = (self.height - h) // 2
        else:
            y = m if pos.startswith("top") else self.height - h - m
        return x, y, w, h

    def _draw_home_button(self, buf: bytearray) -> None:
        rect = self.home_rect
        if rect is None:
            return
        x, y, w, h = rect
        p = Painter(buf, self.width, self.height)
        p.rounded(x, y, w, h, 12, "#202830")
        cx, top = x + w // 2, y + (h - 28) // 2
        for i in range(8):                                    # roof
            p.rect(cx - 2 - i * 2, top + i, 4 + i * 4, 1, TEXT)
        p.rect(cx - 11, top + 8, 22, 20, TEXT)                # house
        p.rect(cx - 4, top + 18, 8, 10, "#202830")            # door

    def home_button_runs(self) -> list[tuple[int, bytes]]:
        """The button as (byte offset, bytes) runs, one per row, drawn once.

        Drawing it with Painter on every video frame costs ~8 ms on the Pi; copying
        these runs in costs almost nothing. Drawn on a black and on a white frame: the
        bytes that agree are the button (the rounded corners leave the video visible).
        """
        if getattr(self, "_home_runs", None) is None:
            runs: list[tuple[int, bytes]] = []
            rect = self.home_rect
            if rect is not None:
                black = bytearray(self.width * self.height * 2)
                white = bytearray(b"\xff" * len(black))
                self._draw_home_button(black)
                self._draw_home_button(white)
                stride = self.width * 2
                x, y, w, h = rect
                for row in range(y, y + h):
                    o = row * stride
                    same = [i for i in range(2 * x, 2 * (x + w), 2)
                            if black[o + i:o + i + 2] == white[o + i:o + i + 2]]
                    if same:
                        a, b = o + same[0], o + same[-1] + 2
                        runs.append((a, bytes(black[a:b])))
            self._home_runs = runs
        return self._home_runs

    def paint_home_button(self, frame: bytes) -> bytes:
        """Draw the Home button onto a video frame."""
        buf = bytearray(frame)
        for offset, run in self.home_button_runs():
            buf[offset:offset + len(run)] = run
        return bytes(buf)

    # ----- status bar -----

    def _status_bar(self, p: Painter) -> None:
        st = self.state
        clock = st.clock()
        self._clock_shown = clock
        x = 16
        if clock:
            p.text(x, 18, clock, 4, TEXT)
            x += cv.text_width(clock, 4) + 22
        else:
            p.text(x, 22, "MIRRORLINK PI", 3, TEXT)
            x += cv.text_width("MIRRORLINK PI", 3) + 22
        if st.signal is not None:                         # signal bars + network type
            for i in range(4):
                h = 6 + i * 5
                p.rect(x + i * 8, 44 - h, 5, h, TEXT if i < st.signal else OFF)
            x += 34
            if st.network:
                p.text(x, 30, st.network, 2, TEXT)
                x += cv.text_width(st.network, 2)
            x += 20
        if st.battery is not None:                        # battery outline, fill, percent
            p.rect(x, 22, 36, 20, DIM)
            p.rect(x + 2, 24, 32, 16, BG)
            p.rect(x + 36, 28, 3, 8, DIM)
            fill = GREEN if st.charging or st.battery > 20 else (
                AMBER if st.battery > 10 else RED)
            p.rect(x + 3, 25, max(1, 30 * st.battery // 100), 14, fill)
            if st.charging:                               # a small bolt across the battery
                for i in range(7):
                    p.rect(x + 20 - i, 25 + i, 3, 1, BG)
                p.rect(x + 14, 32, 7, 1, BG)
                for i in range(7):
                    p.rect(x + 18 - i, 32 + i, 3, 1, BG)
            p.text(x + 46, 26, f"{st.battery}%", 2, TEXT)
        # right-hand buttons
        y, h = 8, 48
        right = self.width - 16
        screen = (right - 64, y, 64, h)
        dnd = (right - 64 - 10 - 64, y, 64, h)
        nxt = (dnd[0] - 24 - 56, y, 56, h)
        play = (nxt[0] - 8 - 56, y, 56, h)
        prev = (play[0] - 8 - 56, y, 56, h)
        for box, key in ((prev, "previous"), (play, "play_pause"), (nxt, "next")):
            p.rounded(*box, 10, BUTTON)
            self._media_icon(p, box, key)
            self.targets.append((box, ("media", key)))
        p.rounded(*dnd, 10, ACTIVE_DND if st.dnd else BUTTON)    # Do Not Disturb: ⊖
        cx, cy = dnd[0] + dnd[2] // 2, y + h // 2
        p.rounded(cx - 12, cy - 12, 24, 24, 12, TEXT if st.dnd is not None else DIM)
        p.rect(cx - 7, cy - 2, 14, 4, ACTIVE_DND if st.dnd else BUTTON)
        self.targets.append((dnd, ("dnd", None)))
        p.rounded(*screen, 10, ACTIVE_SCREEN if st.screen_on else BUTTON)  # phone screen
        cx = screen[0] + screen[2] // 2
        p.rounded(cx - 9, y + 9, 18, 30, 3, TEXT)
        p.rect(cx - 7, y + 13, 14, 22, "#90caf9" if st.screen_on else "#202830")
        self.targets.append((screen, ("screen", None)))

    @staticmethod
    def _triangle(p: Painter, x: int, y: int, h: int, right: bool, colour) -> None:
        for i in range(h):
            w = h // 2 - abs(i - h // 2) + 1
            p.rect(x if right else x + h // 2 + 1 - w, y + i, w, 1, colour)

    def _media_icon(self, p: Painter, box, key: str) -> None:
        x, y, w, h = box
        cx, cy, s = x + w // 2, y + h // 2, 18
        if key == "previous":
            p.rect(cx - 10, cy - s // 2, 3, s, TEXT)
            self._triangle(p, cx - 6, cy - s // 2, s, False, TEXT)
        elif key == "next":
            self._triangle(p, cx - 4, cy - s // 2, s, True, TEXT)
            p.rect(cx + 7, cy - s // 2, 3, s, TEXT)
        else:                                             # play/pause: ▶ ‖
            self._triangle(p, cx - 13, cy - s // 2, s, True, TEXT)
            p.rect(cx + 3, cy - s // 2, 4, s, TEXT)
            p.rect(cx + 10, cy - s // 2, 4, s, TEXT)

    # ----- hit testing -----

    def target_at(self, x: int, y: int) -> Target | None:
        with self._lock:
            targets = list(self.targets)
        for (tx, ty, tw, th), target in targets:
            if tx <= x < tx + tw and ty <= y < ty + th:
                return target
        return None

    def in_home_button(self, x: int, y: int) -> bool:
        rect = self.home_rect
        if rect is None:
            return False
        bx, by, bw, bh = rect
        return bx <= x < bx + bw and by <= y < by + bh
