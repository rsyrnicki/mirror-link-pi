"""Phone mode: mirror an Android phone to the car with scrcpy.

The phone joins the Pi's Wi-Fi hotspot with "Wireless debugging" on. The Pi finds it
(mDNS ``_adb-tls-connect._tcp``, or legacy adb TCP on port 5555), pushes the scrcpy
server, and asks it for a new 800×480 virtual display. The phone streams H.264 of
that display; libavcodec (ctypes, avdecode.py) decodes it into RGB565 frames which
the VNC server sends to the car; the car's touches go back as scrcpy touch events.

The scrcpy server/client protocol is internal to scrcpy and changes between
versions (doc/develop.md in the scrcpy source), so this client targets exactly one
server version, ``SCRCPY_VERSION``; ``prepare-sd.sh --phone`` installs that server
jar (checksum-pinned).

Protocol summary for scrcpy 4.1, forward tunnel, audio off:
  video socket:   dummy byte, 64-byte device name, u32 codec id, then packets:
                  session packet  = u32 (MSB set | client-resized bit), u32 w, u32 h
                  media packet    = u64 pts+flags (bit62 config, bit61 key), u32 size,
                                    payload (Annex-B H.264)
  control socket: client → device messages (touch 32 bytes, keycode 14, start app, …)
"""

from __future__ import annotations

import errno
import logging
import os
import random
import selectors
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .session import Session

log = logging.getLogger(__name__)

SCRCPY_VERSION = "4.1"
REMOTE_JAR = "/data/local/tmp/mlpi-scrcpy-server.jar"
# The server deletes its own jar when it starts (cleanup=true), so the app-list query
# needs its own copy.
REMOTE_LIST_JAR = "/data/local/tmp/mlpi-scrcpy-list.jar"

CODEC_H264 = 0x68323634

# Control message types (scrcpy app/src/control_msg.h, enum order).
MSG_INJECT_KEYCODE = 0
MSG_INJECT_TOUCH_EVENT = 2
MSG_INJECT_SCROLL_EVENT = 3
MSG_BACK_OR_SCREEN_ON = 4
MSG_SET_DISPLAY_POWER = 10
MSG_START_APP = 16
MSG_RESET_VIDEO = 17                # new keyframe (restarts the encoder)

ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID_GENERIC_FINGER = (1 << 64) - 2           # UINT64_C(-2)

KEYCODE_HOME, KEYCODE_BACK, KEYCODE_DPAD_CENTER = 3, 4, 23
MEDIA_KEYCODES = {"play_pause": 85, "next": 87, "previous": 88}   # KEYCODE_MEDIA_*
# `cmd media_session dispatch` names: delivered to the app that is playing, whatever
# display it is on (key events injected into the virtual display don't reach it).
MEDIA_DISPATCH = {"play_pause": "play-pause", "next": "next", "previous": "previous"}

# The car's rotary knob (MirrorLink Part 2, Annex B Table B.1, knob 0).
KNOB_RIGHT, KNOB_LEFT, KNOB_UP, KNOB_DOWN = 0x30000000, 0x30000001, 0x30000002, 0x30000005
KNOB_PUSH = 0x30000008
KNOB_CW, KNOB_CCW = 0x3000000E, 0x3000000F          # rotate z clockwise / anti-clockwise
KNOB_DPAD = {KNOB_UP: 19, KNOB_DOWN: 20, KNOB_LEFT: 21, KNOB_RIGHT: 22}   # KEYCODE_DPAD_*
STATUS_POLL_SECONDS = 30

# Car keys (MirrorLink device keys, Part 2 Annex B, and plain X11 keysyms) → Android.
KEYMAP = {
    0x3000020C: KEYCODE_BACK,          # Device_Backward
    0x3000020D: KEYCODE_HOME,          # Device_Home
    0x30000206: KEYCODE_DPAD_CENTER,   # Device_Ok
    0xFF08: KEYCODE_BACK,              # BackSpace
    0xFF1B: KEYCODE_BACK,              # Escape
    0xFF50: KEYCODE_HOME,              # Home
    0xFF0D: KEYCODE_DPAD_CENTER,       # Return
}

# ---------- control messages (byte layouts checked against scrcpy's unit tests) ----------

def touch_message(action: int, x: int, y: int, width: int, height: int, *,
                  pointer_id: int = POINTER_ID_GENERIC_FINGER, pressure: float = 1.0,
                  action_button: int = 0, buttons: int = 0) -> bytes:
    p = 0xFFFF if pressure >= 1.0 else int(max(0.0, pressure) * 0x10000)
    return struct.pack("!BBQiiHHHII", MSG_INJECT_TOUCH_EVENT, action, pointer_id, x, y,
                       width, height, p, action_button, buttons)


def keycode_message(action: int, keycode: int, *, repeat: int = 0, metastate: int = 0) -> bytes:
    return struct.pack("!BBIII", MSG_INJECT_KEYCODE, action, keycode, repeat, metastate)


def scroll_message(x: int, y: int, width: int, height: int, hscroll: float,
                   vscroll: float, buttons: int = 0) -> bytes:
    """Mouse-wheel event at x, y; 1.0 = one notch. scrcpy sends the amounts as signed
    16-bit fixed point of value/16 (ControlMessageReader.parseInjectScrollEvent)."""
    def fp(v: float) -> int:
        return max(-0x8000, min(0x7FFF, round(v / 16 * 0x8000)))
    return struct.pack("!BiiHHhhi", MSG_INJECT_SCROLL_EVENT, x, y, width, height,
                       fp(hscroll), fp(vscroll), buttons)


def back_or_screen_on_message(action: int) -> bytes:
    return struct.pack("!BB", MSG_BACK_OR_SCREEN_ON, action)


def display_power_message(on: bool) -> bytes:
    """With a virtual display, this powers the phone's *own* screen (scrcpy
    Controller.setDisplayPower); apps keep rendering on the virtual display."""
    return struct.pack("!BB", MSG_SET_DISPLAY_POWER, int(on))


def fit_box(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int]:
    """Where a src_w×src_h picture goes inside dst_w×dst_h keeping its proportions:
    (x, y, w, h). A portrait app on the landscape car screen gets black side bars."""
    scale = min(dst_w / src_w, dst_h / src_h)
    w = max(2, min(dst_w, round(src_w * scale)) // 2 * 2)
    if w < dst_w:
        # A multiple of 16: libswscale writes whole SIMD blocks and would spill a few
        # pixels of picture into the right-hand bar otherwise.
        w = max(16, w // 16 * 16)
    h = max(2, min(dst_h, round(src_h * scale)) // 2 * 2)
    return (dst_w - w) // 2, (dst_h - h) // 2, w, h


def start_app_message(name: str) -> bytes:
    raw = name.encode()[:255]
    return struct.pack("!BB", MSG_START_APP, len(raw)) + raw


def reset_video_message() -> bytes:
    return struct.pack("!B", MSG_RESET_VIDEO)


class LagGuard:
    """Keeps the picture live when decoding can't keep up.

    The decoder runs in the socket loop, so when it is slower than the phone the video
    queues up in the TCP buffers and the phone's encoder: the car then shows (and a tap
    lands on) what the phone did seconds ago, and the delay only grows. Each packet's
    presentation time (phone clock, µs) is compared with its arrival time; the smallest
    difference seen is "live". Once a packet is more than ``max_lag`` s behind that,
    everything up to the next keyframe is dropped undecoded (which drains the queue
    quickly), and the caller asks the phone for a fresh keyframe.
    """

    def __init__(self, max_lag: float) -> None:
        self.max_lag = max_lag
        self.offset: float | None = None
        self.skipping = False
        self.skips = 0
        self.lag = 0.0

    def restart(self) -> None:
        """New encoder session (rotation, reset): its timestamps start over."""
        self.offset = None

    def decode(self, pts_us: int, key: bool, now: float) -> bool:
        """True = decode this packet; False = drop it."""
        if self.skipping:
            if not key:
                return False
            self.skipping = False
            self.offset = None
        offset = now - pts_us / 1e6
        if self.offset is None or offset < self.offset:
            self.offset = offset
        self.lag = offset - self.offset
        if self.max_lag > 0 and self.lag > self.max_lag:
            self.skipping = True
            self.skips += 1
            return False
        return True


# ---------- video stream ----------

class StreamParser:
    """Incremental parser for the scrcpy video socket."""

    def __init__(self, *, dummy_byte: bool = True, device_meta: bool = True) -> None:
        self._buf = bytearray()
        self._stage = "dummy" if dummy_byte else ("device" if device_meta else "codec")
        self._device_meta = device_meta

    def feed(self, data: bytes) -> list[tuple]:
        self._buf += data
        events: list[tuple] = []
        buf = self._buf
        while True:
            if self._stage == "dummy":
                if len(buf) < 1:
                    break
                del buf[:1]
                events.append(("dummy",))
                self._stage = "device" if self._device_meta else "codec"
            elif self._stage == "device":
                if len(buf) < 64:
                    break
                name = bytes(buf[:64]).split(b"\0", 1)[0].decode("utf-8", "replace")
                del buf[:64]
                events.append(("device", name))
                self._stage = "codec"
            elif self._stage == "codec":
                if len(buf) < 4:
                    break
                (codec,) = struct.unpack_from("!I", buf)
                del buf[:4]
                events.append(("codec", codec))
                self._stage = "packets"
            else:
                if len(buf) < 12:
                    break
                (head,) = struct.unpack_from("!Q", buf)
                if head >> 63:                       # session packet
                    _, width, height = struct.unpack_from("!III", buf)
                    events.append(("session", width, height, bool(head >> 32 & 1)))
                    del buf[:12]
                    continue
                (size,) = struct.unpack_from("!I", buf, 8)
                if len(buf) < 12 + size:
                    break
                payload = bytes(buf[12:12 + size])
                del buf[:12 + size]
                config = bool(head >> 62 & 1)
                key = bool(head >> 61 & 1)
                pts = head & ((1 << 61) - 1)
                events.append(("packet", config, key, pts, payload))
        return events


class Decoder:
    """Packet-level H.264 decoding (libavcodec via ctypes, see avdecode.py).

    Each scrcpy packet is one access unit; handing it straight to the decoder gives the
    frame back immediately, scaled/converted to RGB565LE at the car's screen size.
    """

    def __init__(self, width: int, height: int, on_frame: Callable[[bytes], None], *,
                 codec: str = "", threads: int = 1,
                 canvas: tuple[int, int, int, int] | None = None,
                 overlay: list[tuple[int, bytes]] | None = None) -> None:
        from .avdecode import AvDecoder
        self.av = AvDecoder(width, height, codec=codec or "h264", threads=threads,
                            canvas=canvas, overlay=overlay)
        self.on_frame = on_frame
        self.frames = 0
        self.errors = 0

    def write(self, data: bytes) -> None:
        from .avdecode import DecoderError
        try:
            pictures = self.av.decode(data)
        except DecoderError as exc:
            self.errors += 1
            log.debug("decode error: %s", exc)
            return
        for picture in pictures:
            self.frames += 1
            self.on_frame(picture)

    def stop(self) -> None:
        self.av.close()


# ---------- mDNS: find the phone's wireless-debugging port ----------

MDNS_ADDR = ("224.0.0.251", 5353)
ADB_TLS_SERVICE = "_adb-tls-connect._tcp.local"
ADB_PAIRING_SERVICE = "_adb-tls-pairing._tcp.local"   # while "Pair with code" is open
PAIR_PAGE_AFTER = 20.0     # s on the hotspot without adb before the car shows pairing
TYPE_A, TYPE_PTR, TYPE_SRV = 1, 12, 33


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p) + b"\0"


def mdns_query(service: str = ADB_TLS_SERVICE) -> bytes:
    # One-shot query (sent from an ephemeral port): responders answer by unicast to the
    # sender (RFC 6762 §6.7). Class IN with the unicast-response bit.
    return struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + _encode_name(service) + struct.pack(
        "!HH", TYPE_PTR, 0x8001)


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels, jumped, end = [], False, offset
    for _ in range(128):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack_from("!H", data, offset)[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            jumped = True
            offset = pointer
            continue
        labels.append(data[offset + 1:offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length
    return ".".join(labels), (end if jumped else offset)


def parse_mdns(data: bytes) -> dict:
    """Return {"ptr": [...], "srv": {instance: (target, port)}, "a": {host: ip}}."""
    out: dict = {"ptr": [], "srv": {}, "a": {}}
    _, _, qd, an, ns, ar = struct.unpack_from("!HHHHHH", data)
    offset = 12
    for _ in range(qd):
        _, offset = _read_name(data, offset)
        offset += 4
    for _ in range(an + ns + ar):
        name, offset = _read_name(data, offset)
        rtype, _, _, rdlen = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        rdata_at = offset
        offset += rdlen
        if rtype == TYPE_PTR:
            out["ptr"].append(_read_name(data, rdata_at)[0])
        elif rtype == TYPE_SRV:
            _, _, port = struct.unpack_from("!HHH", data, rdata_at)
            out["srv"][name] = (_read_name(data, rdata_at + 6)[0], port)
        elif rtype == TYPE_A and rdlen == 4:
            out["a"][name] = socket.inet_ntoa(data[rdata_at:rdata_at + 4])
    return out


# Wireless debugging listens on a kernel-chosen port: Linux's ephemeral range.
ADB_PORT_RANGE = (32768, 60999)


def scan_open_ports(ip: str, first: int, last: int, *, concurrency: int = 400,
                    timeout: float = 0.6, deadline: float = 30.0) -> list[int]:
    """TCP-connect scan of ip:first..last; returns the ports that accepted.

    Finds the phone's Wireless debugging port when mDNS doesn't: Android drops
    multicast (so mDNS queries) while the screen is off, but plain unicast TCP gets
    through. Closed ports answer with a reset at once, so ~28 000 ports take a few
    seconds on the hotspot.
    """
    sel = selectors.DefaultSelector()
    pending: dict[socket.socket, tuple[int, float]] = {}
    found: list[int] = []
    ports = iter(range(first, last + 1))
    stop_at = time.monotonic() + deadline
    exhausted = False
    try:
        while (pending or not exhausted) and time.monotonic() < stop_at:
            while not exhausted and len(pending) < concurrency:
                port = next(ports, None)
                if port is None:
                    exhausted = True
                    break
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setblocking(False)
                rc = s.connect_ex((ip, port))
                if rc in (0, errno.EINPROGRESS, errno.EWOULDBLOCK):
                    pending[s] = (port, time.monotonic() + timeout)
                    sel.register(s, selectors.EVENT_WRITE)
                else:
                    s.close()
            for key, _ in sel.select(timeout=0.05):
                s = key.fileobj
                port, _t = pending.pop(s)
                sel.unregister(s)
                if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    found.append(port)
                s.close()
            now = time.monotonic()
            for s, (_port, until) in list(pending.items()):
                if now > until:                      # no answer at all: give up on it
                    del pending[s]
                    sel.unregister(s)
                    s.close()
    finally:
        for s in pending:
            s.close()
        sel.close()
    return sorted(found)


def discover_adb_tls(interface: str, local_ip: str, *, timeout: float = 2.0,
                     service: str = ADB_TLS_SERVICE) -> list[tuple[str, int]]:
    """Ask the Wi-Fi network for wireless-debugging endpoints: [(ip, port), ...].
    ``service``: the connect service, or ADB_PAIRING_SERVICE for the pairing dialog."""
    found: list[tuple[str, int]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        if interface:
            sock.setsockopt(socket.SOL_SOCKET, 25, interface.encode())   # SO_BINDTODEVICE
        if local_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.5)
        query = mdns_query(service)
        deadline = time.monotonic() + timeout
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                sock.sendto(query, MDNS_ADDR)
                next_send = time.monotonic() + 0.7
            try:
                data, (src, _) = sock.recvfrom(9000)
            except TimeoutError:
                continue
            try:
                answer = parse_mdns(data)
            except (struct.error, IndexError):
                continue
            for instance, (target, port) in answer["srv"].items():
                if not instance.rstrip(".").endswith(service):
                    continue                  # e.g. the other adb service in the same answer
                ip = answer["a"].get(target, src)
                if (ip, port) not in found:
                    found.append((ip, port))
    except OSError as exc:
        log.debug("mDNS discovery failed: %s", exc)
    finally:
        sock.close()
    return found


# ---------- adb ----------

class Adb:
    """Thin wrapper around the adb binary with its own key directory and server port."""

    def __init__(self, binary: str = "adb", *, home: str = "", server_port: int = 0) -> None:
        self.binary = binary
        self.env = dict(os.environ)
        if home:
            # adb keeps its key in $HOME/.android/adbkey (ANDROID_USER_HOME overrides).
            self.env["HOME"] = home
            self.env["ANDROID_USER_HOME"] = str(Path(home) / ".android")
        if server_port:
            self.env["ANDROID_ADB_SERVER_PORT"] = str(server_port)

    def run(self, *args: str, serial: str = "",
            timeout: float = 20.0) -> subprocess.CompletedProcess:
        cmd = [self.binary] + (["-s", serial] if serial else []) + list(args)
        try:
            return subprocess.run(cmd, env=self.env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", "timeout")
        except OSError as exc:                  # adb not installed
            return subprocess.CompletedProcess(cmd, 127, "", f"adb unavailable: {exc}")

    def connect(self, target: str) -> bool:
        out = self.run("connect", target, timeout=8)
        text = (out.stdout + out.stderr).lower()
        return "connected to" in text and "cannot" not in text and "failed" not in text

    def pair(self, target: str, code: str) -> tuple[bool, str]:
        """`adb pair HOST:PORT CODE` (Android 11+ Wireless debugging)."""
        out = self.run("pair", target, code, timeout=25)
        text = (out.stdout + out.stderr).strip()
        return "successfully paired" in text.lower(), text

    def state(self, serial: str) -> str:
        out = self.run("get-state", serial=serial, timeout=8)
        if out.returncode == 0:
            return out.stdout.strip()
        text = out.stderr.strip().lower()
        for word in ("unauthorized", "offline", "not found"):
            if word in text:
                return word
        return text or "error"

    def devices(self) -> list[tuple[str, str]]:
        out = self.run("devices", timeout=8)
        rows = []
        for line in out.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                rows.append((parts[0], parts[1]))
        return rows

    def popen(self, *args: str, serial: str, stdout, stderr) -> subprocess.Popen:
        return subprocess.Popen([self.binary, "-s", serial, *args], env=self.env,
                                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)


# ---------- configuration of the Pi's hotspot ----------

@dataclass
class HotspotSettings:
    interface: str
    address: str            # e.g. 192.168.8.1/24
    ssid: str
    password: str
    country: str
    channel: int


HOTSPOT_CONNECTION = "mlpi-hotspot"


def hotspot_commands(s: HotspotSettings, exists: bool) -> list[list[str]]:
    """nmcli commands that create/update and start the hotspot connection."""
    settings = [
        "connection.interface-name", s.interface,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "100",
        "802-11-wireless.ssid", s.ssid,
        "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg",
        "802-11-wireless.channel", str(s.channel),
        # 2 = off. The Pi's Wi-Fi chip saves power by default, which on Raspberry Pis
        # causes stalls of seconds; the car powers us, so there is nothing to save.
        "802-11-wireless.powersave", "2",
        "802-11-wireless-security.key-mgmt", "wpa-psk",
        "802-11-wireless-security.proto", "rsn",
        "802-11-wireless-security.pairwise", "ccmp",
        "802-11-wireless-security.group", "ccmp",
        "802-11-wireless-security.psk", s.password,
        # Our own DHCP server runs on the interface; no gateway/DNS is offered, so the
        # phone keeps using mobile data for the internet.
        "ipv4.method", "manual",
        "ipv4.addresses", s.address,
        "ipv6.method", "disabled",
    ]
    if exists:
        cmds = [["nmcli", "connection", "modify", HOTSPOT_CONNECTION, *settings]]
    else:
        cmds = [["nmcli", "connection", "add", "type", "wifi", "con-name", HOTSPOT_CONNECTION,
                 "ifname", s.interface, "ssid", s.ssid], ["nmcli", "connection", "modify",
                                                           HOTSPOT_CONNECTION, *settings]]
    return cmds + [["nmcli", "connection", "up", HOTSPOT_CONNECTION]]


def ensure_hotspot(s: HotspotSettings, *, run=subprocess.run) -> str:
    """Create/update and start the Wi-Fi hotspot. Returns a short status text."""
    if not 8 <= len(s.password) <= 63:
        return "no wifi_password (8-63 chars) configured: hotspot not started"
    try:
        if s.country:
            # Sets the regulatory domain and unblocks Wi-Fi (rfkill) on Pi OS.
            run(["raspi-config", "nonint", "do_wifi_country", s.country],
                capture_output=True, timeout=30)
        names = run(["nmcli", "-t", "-f", "NAME", "connection", "show"],
                    capture_output=True, text=True, timeout=15).stdout.split()
        for cmd in hotspot_commands(s, HOTSPOT_CONNECTION in names):
            out = run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                return f"{' '.join(cmd[:3])} failed: {(out.stderr or out.stdout).strip()[:200]}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"hotspot setup failed: {exc}"
    try:        # belt and braces: the driver's power saving off directly as well
        run(["iw", "dev", s.interface, "set", "power_save", "off"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass    # no iw: NetworkManager's powersave setting above still applies
    return f"hotspot {s.ssid!r} up on {s.interface}"


# ---------- the phone link ----------

class PhoneDisconnected(Exception):
    pass


class PhoneLink:
    """Finds the phone, runs scrcpy and feeds the decoded video into ``frame``."""

    def __init__(self, cfg, frame, switch, *, session: Session | None = None,
                 candidates: Callable[[], list[str]] = list, serial: str = "",
                 local_ip: str = "", adb: Adb | None = None) -> None:
        self.cfg = cfg
        self.frame = frame
        self.switch = switch
        self.session = session
        self.candidates = candidates
        self.fixed_serial = serial
        self.local_ip = local_ip
        self.adb = adb or Adb(cfg.adb, home=cfg.adb_home)
        self.status = "waiting for the phone on Wi-Fi"
        self._stop = threading.Event()
        self._control: socket.socket | None = None
        self._control_lock = threading.Lock()
        self._video_size = (frame.width, frame.height)
        self._video_box = (0, 0, frame.width, frame.height)   # where the video sits
        self._down = False
        self._last_pos = (-1, -1)
        self._procs: list[subprocess.Popen] = []
        # The Pi's own launcher (launcher.py): tiles, and a Home button over the video.
        self.launcher = None
        if getattr(cfg, "launcher", False) and switch is not None:
            from .launcher import Launcher, parse_apps
            self.launcher = Launcher(switch.new_video_frame(), parse_apps(cfg.apps),
                                     home_button=getattr(cfg, "home_button", "right"))
        # Pairing from the car screen (pairing.py) when the phone doesn't know our key.
        self.pairing = None
        if switch is not None:
            from .pairing import PairingPage
            self.pairing = PairingPage(switch.new_video_frame())
        self._pair_press = None
        self._pair_ip = ""
        self._unreachable_since: float | None = None
        self._pairing_dismissed_until = 0.0
        self._tile_press: tuple[int, int] | None = None
        self._home_press = False
        self._show_video_on_frame = False
        self.device_name = ""
        self._serial = ""
        self._last_scan = float("-inf")

    # ----- helpers -----

    def _event(self, kind: str, **fields) -> None:
        if self.session:
            self.session.event(kind, **fields)

    def _set_status(self, text: str) -> None:
        if text != self.status:
            log.info("phone: %s", text)
            self.status = text
            self._event("phone_status", status=text)

    # ----- input from the car -----

    def _send(self, message: bytes) -> None:
        with self._control_lock:
            sock = self._control
            if sock is None:
                return
            try:
                sock.sendall(message)
            except OSError as exc:
                log.info("phone control send failed: %s", exc)

    def show_launcher(self) -> None:
        if self.launcher:
            self.launcher.status = self.device_name
            self.launcher.go(-1)
            self.switch.show(self.launcher.frame)
            self._event("phone_launcher")

    CONNECTIVITY_KEYS = ("Active default network", "NetworkAgentInfo", "everValidated",
                         "acceptUnvalidated", "explicitlySelected", "mobile_data_always_on",
                         "Bad Wi-Fi avoidance", "Avoid bad wifi setting")

    def _avoid_bad_wifi(self, serial: str) -> None:
        """Let mobile data stay the phone's internet while it is on the Pi's Wi-Fi.

        Android's "avoid bad Wi-Fi" (Settings.Global.NETWORK_AVOID_BAD_WIFI) is unset
        on e.g. the Galaxy A56, which means "get stuck": with the device config that
        actively prefers bad Wi-Fi, a Wi-Fi without internet becomes the default
        network and apps lose the internet. 1 = prefer validated mobile data. It is
        the same switch as "Switch to mobile data automatically" on stock Android;
        undo with `adb shell settings delete global network_avoid_bad_wifi`.
        """
        key = "network_avoid_bad_wifi"
        before = self.adb.run("shell", "settings", "get", "global", key, serial=serial,
                              timeout=15).stdout.strip()
        if before == "1":
            return
        put = self.adb.run("shell", "settings", "put", "global", key, "1", serial=serial,
                           timeout=15)
        self._event("phone_avoid_bad_wifi", before=before, rc=put.returncode,
                    stderr=(put.stderr or "")[:200])

    def _learn_bt_address(self, serial: str) -> None:
        """Remember the phone's Bluetooth address on the card for the next boot's
        Bluetooth audio entries (the car asks for them before the phone connects)."""
        from . import btaddr
        if not self.session:
            return
        address = ""
        for cmd in (("settings", "get", "secure", "bluetooth_address"),
                    ("dumpsys", "bluetooth_manager")):
            out = self.adb.run("shell", *cmd, serial=serial, timeout=20)
            address = btaddr.parse_phone_output(out.stdout or "")
            if address:
                break
        root = self.session.directory.parent.parent        # <root>/sessions/NNNN
        try:
            changed = btaddr.remember(root, address)
        except OSError:
            changed = False
        self._event("phone_bt_address", found=bool(address), changed=changed)

    def _record_connectivity(self, serial: str) -> None:
        """Snapshot which network the phone uses for the internet (Wi-Fi to the Pi has
        none), 20 s after connecting: full dump to phone-connectivity.txt, the key
        lines as a phone_connectivity event."""
        if getattr(self.cfg, "avoid_bad_wifi", True):
            self._avoid_bad_wifi(serial)
        self._learn_bt_address(serial)
        if self._stop.wait(20):
            return
        dump = self.adb.run("shell", "dumpsys", "connectivity", serial=serial, timeout=30)
        always_on = self.adb.run("shell", "settings", "get", "global",
                                 "mobile_data_always_on", serial=serial, timeout=15)
        text = (dump.stdout or "") + f"\nmobile_data_always_on={always_on.stdout.strip()}\n"
        if self.session:
            try:
                (self.session.directory / "phone-connectivity.txt").write_text(text)
            except OSError:
                pass
        keys = [line.strip()[:300] for line in text.splitlines()
                if any(k in line for k in self.CONNECTIVITY_KEYS)]
        self._event("phone_connectivity", lines=keys[:40])

    def _poll_status(self, serial: str, stop: threading.Event) -> None:
        """Feed the launcher's status bar: one adb call every 30 s, clock ticks between."""
        from .phonestatus import POLL_COMMAND, parse_poll
        while not stop.is_set() and not self._stop.is_set():
            out = self.adb.run("shell", POLL_COMMAND, serial=serial, timeout=15)
            if out.returncode == 0 and out.stdout:
                self.launcher.set_state(**parse_poll(out.stdout))
            for _ in range(STATUS_POLL_SECONDS // 5):
                if stop.wait(5) or self._stop.is_set():
                    return
                self.launcher.tick()

    def _media_key(self, key: str) -> None:
        """Play/pause, next, previous for the app that is playing. Falls back to a key
        event on the phone's main display if media_session isn't available."""
        out = self.adb.run("shell", "cmd", "media_session", "dispatch", MEDIA_DISPATCH[key],
                           serial=self._serial, timeout=10)
        ok = out.returncode == 0 and "error" not in (out.stdout + out.stderr).lower()
        if not ok:
            out = self.adb.run("shell", "input", "-d", "0", "keyevent",
                               str(MEDIA_KEYCODES[key]), serial=self._serial, timeout=10)
        self._event("phone_media_key", key=key, via="media_session" if ok else "keyevent",
                    rc=out.returncode, output=(out.stdout + out.stderr)[:200])

    def _set_dnd(self, on: bool) -> None:
        out = self.adb.run("shell", "cmd", "notification", "set_dnd", "on" if on else "off",
                           serial=self._serial, timeout=15)
        self._event("phone_dnd", on=on, rc=out.returncode, stderr=(out.stderr or "")[:200])

    def _load_app_list(self, serial: str) -> None:
        """Ask the phone for its launchable apps (scrcpy's list_apps) for the
        launcher's "All apps" pages. Runs in the background once per connection."""
        from .launcher import parse_app_list
        push = self.adb.run("push", self.cfg.server_jar, REMOTE_LIST_JAR, serial=serial,
                            timeout=60)
        out = self.adb.run("shell", f"CLASSPATH={REMOTE_LIST_JAR}", "app_process", "/",
                           "com.genymobile.scrcpy.Server", SCRCPY_VERSION, "list_apps=true",
                           "log_level=info", serial=serial, timeout=90)
        apps = parse_app_list(out.stdout)
        self._event("phone_app_list", count=len(apps), push_rc=push.returncode,
                    rc=out.returncode, stderr=(out.stderr or "")[:500],
                    stdout_head=(out.stdout or "")[:300])
        if apps and self.launcher:
            self.launcher.set_all_apps(apps)

    def open_app(self, app) -> None:
        self._send(start_app_message(app.package))
        self._event("phone_open_app", name=app.name, package=app.package)
        self.switch.show(self.frame)

    def _launcher_pointer(self, x: int, y: int, buttons: int) -> bool:
        """Handle input that belongs to the launcher; True if consumed."""
        if not self.launcher:
            return False
        pressed = bool(buttons & 1)
        if self.switch.showing(self.launcher.frame):
            if pressed:                               # act on release, like a button
                if self._tile_press is None:
                    self._tile_press = (x, y)
                    self.launcher.clear_focus()       # touch users don't need the frame
            elif self._tile_press is not None:
                target = self.launcher.target_at(*self._tile_press)
                self._tile_press = None
                if target is not None and self.launcher.target_at(x, y) == target:
                    self._launcher_action(target)
            return True
        if self._home_press:                          # swallow until the finger lifts
            if not pressed:
                self._home_press = False
                if self.launcher.in_home_button(x, y):
                    self.show_launcher()
            return True
        if pressed and not self._down and self.launcher.in_home_button(x, y):
            self._home_press = True
            return True
        return False

    def _launcher_action(self, target) -> None:
        kind, value = target
        launcher = self.launcher
        if kind == "app":
            self.open_app(value)
        elif kind == "all":
            launcher.go(0)
        elif kind == "home":
            launcher.go(-1)
        elif kind == "prev":
            launcher.go(launcher.page - 1)
        elif kind == "next":
            launcher.go(launcher.page + 1)
        elif kind == "media":
            threading.Thread(target=self._media_key, args=(value,), name="phone-media",
                             daemon=True).start()
        elif kind == "dnd":
            on = not launcher.state.dnd
            launcher.set_state(dnd=on)                # show it at once; the poll confirms
            threading.Thread(target=self._set_dnd, args=(on,), name="phone-dnd",
                             daemon=True).start()
        elif kind == "screen":
            on = not launcher.state.screen_on
            self._send(display_power_message(on))
            launcher.set_state(screen_on=on)
            self._event("phone_screen", on=on)

    def on_pointer(self, x: int, y: int, buttons: int) -> None:
        if self.pairing and self.switch.showing(self.pairing.frame):
            self._pairing_pointer(x, y, buttons)
            return
        if self._launcher_pointer(x, y, buttons):
            return
        vw, vh = self._video_size
        bx, by, bw, bh = self._video_box
        pressed = bool(buttons & 1)
        inside = bx <= x < bx + bw and by <= y < by + bh
        if pressed and not self._down and not inside:
            return                                   # a tap on the black side bars
        fx = min(vw - 1, max(0, (x - bx) * vw // bw))
        fy = min(vh - 1, max(0, (y - by) * vh // bh))
        if pressed and not self._down:
            action = ACTION_DOWN
        elif pressed:
            if (fx, fy) == self._last_pos:
                return
            action = ACTION_MOVE
        elif self._down:
            action = ACTION_UP
        else:
            return
        self._down = pressed
        self._last_pos = (fx, fy)
        self._send(touch_message(action, fx, fy, vw, vh, pressure=1.0 if pressed else 0.0))

    def on_key(self, keysym: int, down: bool) -> None:
        if self._knob(keysym, down):
            return
        keycode = KEYMAP.get(keysym)
        if keycode == KEYCODE_HOME and self.launcher:
            if not down:
                self.show_launcher()
            return
        if keycode is None:
            self._event("phone_key_unmapped", keysym=f"0x{keysym:08x}", down=down)
            return
        self._send(keycode_message(ACTION_DOWN if down else ACTION_UP, keycode))

    def _knob(self, keysym: int, down: bool) -> bool:
        """The car's rotary knob: moves the highlight on the launcher (push opens), and
        acts as a scroll wheel inside apps (zoom in Maps, scrolling lists)."""
        if not KNOB_RIGHT <= keysym <= KNOB_CCW:
            return False
        launcher = self.launcher
        if launcher and self.switch.showing(launcher.frame):
            if keysym in (KNOB_CW, KNOB_RIGHT, KNOB_DOWN) and down:
                launcher.move_focus(+1)
            elif keysym in (KNOB_CCW, KNOB_LEFT, KNOB_UP) and down:
                launcher.move_focus(-1)
            elif keysym == KNOB_PUSH and not down:
                target = launcher.focused()
                if target is not None:
                    self._launcher_action(target)
            return True
        if keysym in (KNOB_CW, KNOB_CCW):
            if down:
                vw, vh = self._video_size
                notch = -1.0 if keysym == KNOB_CW else 1.0     # clockwise = wheel down
                if getattr(self.cfg, "knob_invert", False):
                    notch = -notch
                self._send(scroll_message(vw // 2, vh // 2, vw, vh, 0.0, notch))
        elif keysym == KNOB_PUSH:
            self._send(keycode_message(ACTION_DOWN if down else ACTION_UP,
                                       KEYCODE_DPAD_CENTER))
        elif keysym in KNOB_DPAD:
            self._send(keycode_message(ACTION_DOWN if down else ACTION_UP, KNOB_DPAD[keysym]))
        else:
            self._event("phone_key_unmapped", keysym=f"0x{keysym:08x}", down=down)
        return True

    # ----- finding the phone -----

    def _find_device(self) -> str:
        if self.fixed_serial:
            state = self.adb.state(self.fixed_serial)
            if state == "device":
                return self.fixed_serial
            self._set_status(f"adb {self.fixed_serial}: {state}")
            return ""
        for serial, state in self.adb.devices():
            if state == "device":
                return serial
        ips = self.candidates()
        if not ips:
            self._set_status("waiting for the phone on Wi-Fi")
            return ""
        for ip in ips:
            targets = [f"{ip}:{self.cfg.legacy_port}"] if self.cfg.legacy_port else []
            last = self._remembered_port()
            if last:
                targets.append(f"{ip}:{last}")
            self._set_status(f"looking for wireless debugging on {ip}")
            announced = [f"{h}:{p}" for h, p in discover_adb_tls(
                self.cfg.interface, self.local_ip) if h == ip]
            found = self._try_targets(targets + announced)
            if found:
                return found
            if announced and self.pairing and \
                    time.monotonic() >= self._pairing_dismissed_until:
                # Wireless debugging is on (the phone announced it) but refuses our
                # key: it doesn't know this Pi. Ask for pairing right away.
                self._show_pairing(ip)
                self._offer_pairing(ip)
                continue
            # mDNS got no answer (screen off?): look for the port directly, at most
            # every 30 s so the phone isn't kept busy.
            if self._offer_pairing(ip):
                continue
            if time.monotonic() - self._last_scan >= 30:
                self._last_scan = time.monotonic()
                t0 = time.monotonic()
                ports = scan_open_ports(ip, *ADB_PORT_RANGE)
                self._event("phone_port_scan", ip=ip, open=ports[:10],
                            seconds=round(time.monotonic() - t0, 1))
                found = self._try_targets([f"{ip}:{p}" for p in ports[:10]])
                if found:
                    return found
                if ports and self.pairing and \
                        time.monotonic() >= self._pairing_dismissed_until:
                    self._show_pairing(ip)                 # debugging on, key refused
        if self.status.startswith("looking"):
            self._set_status("phone on Wi-Fi, but wireless debugging is off")
        if ips and self._unreachable_since is None:
            self._unreachable_since = time.monotonic()
        if (ips and self.pairing and time.monotonic() - self._unreachable_since >= PAIR_PAGE_AFTER
                and time.monotonic() >= self._pairing_dismissed_until):
            self._show_pairing(ips[0])
        return ""

    # ----- pairing from the car screen -----

    def _offer_pairing(self, ip: str) -> bool:
        """If the phone's "Pair with pairing code" dialog is open, show the number pad
        with its port filled in. True = pairing page is up (skip the port scan)."""
        if not self.pairing:
            return False
        found = [p for h, p in discover_adb_tls(self.cfg.interface, self.local_ip,
                                                timeout=1.5, service=ADB_PAIRING_SERVICE)
                 if h == ip]
        if found:
            self.pairing.set_port(found[0])
            self._show_pairing(ip)
            return True
        if not self.pairing.busy:
            self.pairing.forget_port()                    # dialog closed: port is stale
        return False

    def _show_pairing(self, ip: str) -> None:
        self._pair_ip = ip
        if not self.switch.showing(self.pairing.frame):
            self.switch.show(self.pairing.frame)
            self._event("phone_pairing_shown", ip=ip, port=self.pairing.port)
        self._set_status("phone doesn't know this Pi: pair it on the car screen")

    def _pairing_pointer(self, x: int, y: int, buttons: int) -> None:
        pressed = bool(buttons & 1)
        if pressed:
            if self._pair_press is None:
                self._pair_press = self.pairing.target_at(x, y)
            return
        target, self._pair_press = self._pair_press, None
        if target is None or self.pairing.target_at(x, y) != target:
            return
        action = self.pairing.press(target)
        if action == "later":
            self._pairing_dismissed_until = time.monotonic() + 120
            self.pairing.reset()
            self.switch.show(self.switch.canvas)
        elif action == "pair":
            threading.Thread(target=self._pair, args=(self._pair_ip, self.pairing.port,
                                                      self.pairing.code),
                             name="phone-pair", daemon=True).start()

    def _pair(self, ip: str, port: str, code: str) -> None:
        from .pairing import BAD, GOOD
        self.pairing.busy = True
        self.pairing.set_message("PAIRING ...")
        ok, text = self.adb.pair(f"{ip}:{port}", code)
        self._event("phone_pair", target=f"{ip}:{port}", ok=ok, output=text[:300])
        if ok:
            self.pairing.set_message("PAIRED. CONNECTING ...", GOOD)
            self._unreachable_since = None
            self._last_scan = float("-inf")
            self._stop.wait(1.5)
            self.pairing.reset()
            self.pairing.forget_port()
            if self.switch.showing(self.pairing.frame):
                self.switch.show(self.switch.canvas)
        else:
            self.pairing.busy = False
            self.pairing.code = ""
            self.pairing.field = "code"
            self.pairing.set_message("PAIRING FAILED: CHECK CODE AND PORT", BAD)

    def _try_targets(self, targets: list[str]) -> str:
        for target in dict.fromkeys(targets):            # unique, in order
            if not self.adb.connect(target):
                continue
            state = self.adb.state(target)
            self._event("phone_adb_connect", target=target, state=state)
            if state == "device":
                self._remember_port(target)
                self._unreachable_since = None
                return target
            if state == "unauthorized":
                self._set_status("phone refused adb: pair it (docs/phone-mode.md)")
        return ""

    def _port_file(self) -> Path | None:
        if not self.session:
            return None
        return self.session.directory.parent.parent / "phone-adb-port"

    def _remembered_port(self) -> int:
        path = self._port_file()
        try:
            return int(path.read_text().strip()) if path else 0
        except (OSError, ValueError):
            return 0

    def _remember_port(self, target: str) -> None:
        """Wireless debugging keeps its port until it's switched off; next time we
        try that port first."""
        path = self._port_file()
        port = target.rpartition(":")[2]
        if path and port.isdigit() and int(port) != self.cfg.legacy_port:
            try:
                path.write_text(port + "\n")
            except OSError:
                pass

    # ----- one streaming session -----

    def _free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _server_args(self, scid: int) -> list[str]:
        c = self.cfg
        w, h = self.frame.width, self.frame.height
        args = [f"CLASSPATH={REMOTE_JAR}", "app_process", "/", "com.genymobile.scrcpy.Server",
                SCRCPY_VERSION, f"scid={scid:08x}", "log_level=info", "tunnel_forward=true",
                "audio=false", "control=true", "cleanup=true", "video_codec=h264",
                f"max_fps={c.max_fps}", f"video_bit_rate={c.bit_rate}",
                f"new_display={w}x{h}/{c.dpi}",
                f"vd_system_decorations={str(c.system_decorations).lower()}",
                "clipboard_autosync=false", "display_ime_policy=local"]
        if c.keep_active:
            args.append("keep_active=true")
        return args

    def _connect_video(self, port: int, deadline: float) -> socket.socket:
        while time.monotonic() < deadline and not self._stop.is_set():
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            sock.settimeout(3)
            try:
                if sock.recv(1):         # the dummy byte: the server is really listening
                    return sock
            except OSError:
                pass
            sock.close()
            time.sleep(0.3)
        raise PhoneDisconnected("scrcpy server did not start")

    def _stream(self, serial: str) -> None:
        c = self.cfg
        directory = self.session.directory if self.session else None
        self._set_status(f"starting scrcpy on {serial}")
        push = self.adb.run("push", c.server_jar, REMOTE_JAR, serial=serial, timeout=60)
        if push.returncode != 0:
            raise PhoneDisconnected(f"push failed: {push.stderr.strip()[:200]}")
        scid = random.getrandbits(31)
        port = self._free_port()
        fwd = self.adb.run("forward", f"tcp:{port}", f"localabstract:scrcpy_{scid:08x}",
                           serial=serial)
        if fwd.returncode != 0:
            raise PhoneDisconnected(f"forward failed: {fwd.stderr.strip()[:200]}")
        log_file = open(directory / "phone-server.log", "ab") if directory else subprocess.DEVNULL
        server = self.adb.popen("shell", *self._server_args(scid), serial=serial,
                                stdout=log_file, stderr=subprocess.STDOUT)
        self._procs.append(server)
        decoder: Decoder | None = None
        decoder_box: tuple[int, int, int, int] | None = None
        video = control = None
        try:
            video = self._connect_video(port, time.monotonic() + 20)
            control = socket.create_connection(("127.0.0.1", port), timeout=5)
            with self._control_lock:
                self._control = control
            threading.Thread(target=self._drain, args=(control,), daemon=True).start()
            if self.launcher:
                threading.Thread(target=self._load_app_list, args=(serial,),
                                 name="phone-apps", daemon=True).start()
            threading.Thread(target=self._record_connectivity, args=(serial,),
                             name="phone-net", daemon=True).start()
            self._serial = serial
            status_stop = threading.Event()
            if self.launcher:
                threading.Thread(target=self._poll_status, args=(serial, status_stop),
                                 name="phone-status", daemon=True).start()
            if c.start_app:
                self._send(start_app_message(c.start_app))
                self._show_video_on_frame = True
            elif self.launcher:
                self.show_launcher()
            else:
                self._show_video_on_frame = True
            if getattr(c, "screen_off", False):
                # Locking the phone blanks the virtual display; a dark (but unlocked)
                # screen doesn't, and saves battery.
                self._send(display_power_message(False))
            if self.launcher:
                self.launcher.set_state(screen_on=not getattr(c, "screen_off", False))
            video.settimeout(10)
            parser = StreamParser(dummy_byte=False)
            config_packet = b""        # SPS/PPS: kept for decoder restarts
            pending_config = b""       # merged into the next frame, as scrcpy's client does
            first = True
            stats_t, stats_frames, frames = time.monotonic(), 0, 0
            lag_max = 0.0
            guard = LagGuard(c.max_lag)

            def on_frame(data: bytes) -> None:
                nonlocal first, frames
                frames += 1
                self.frame.update(data)
                if first:
                    first = False
                    self._event("phone_first_frame")
                if self._show_video_on_frame:
                    self._show_video_on_frame = False
                    self.switch.show(self.frame)

            while not self._stop.is_set():
                try:
                    data = video.recv(262144)
                except TimeoutError:
                    if server.poll() is not None:
                        raise PhoneDisconnected("scrcpy server exited") from None
                    continue
                if not data:
                    raise PhoneDisconnected("video stream closed")
                for ev in parser.feed(data):
                    kind = ev[0]
                    if kind == "device":
                        self.device_name = ev[1]
                        self._event("phone_device", name=ev[1])
                        if self.launcher and self.switch.showing(self.launcher.frame):
                            self.show_launcher()          # redraw with the phone's name
                    elif kind == "codec" and ev[1] != CODEC_H264:
                        raise PhoneDisconnected(f"unexpected codec 0x{ev[1]:08x}")
                    elif kind == "session":
                        _, w, h, _resized = ev
                        self._video_size = (w, h)
                        # The virtual display rotates with its content: a portrait-only
                        # app turns it to e.g. 480x800. Keep the proportions.
                        box = fit_box(w, h, self.frame.width, self.frame.height)
                        self._video_box = box
                        self._event("phone_session", width=w, height=h, box=list(box))
                        self._set_status(f"streaming {serial} {w}x{h}")
                        # Same size (e.g. after a video reset): keep the decoder, the new
                        # SPS/PPS + keyframe restart it. Only a new size needs a new one.
                        if decoder is None or box != decoder_box:
                            if decoder:
                                decoder.stop()
                            try:
                                # Portrait apps: libswscale draws the picture straight
                                # into the middle of a black full-size frame.
                                canvas = (self.frame.width, self.frame.height, box[0], box[1])
                                overlay = (self.launcher.home_button_runs()
                                           if self.launcher else None)
                                decoder = Decoder(box[2], box[3], on_frame, canvas=canvas,
                                                  overlay=overlay, codec=c.decoder,
                                                  threads=c.decoder_threads)
                            except Exception as exc:  # noqa: BLE001 - e.g. no libavcodec
                                raise PhoneDisconnected(f"decoder unavailable: {exc}") from None
                            decoder_box = box
                        pending_config = config_packet
                        guard.restart()
                    elif kind == "packet":
                        _, is_config, key, pts, payload = ev
                        if is_config:
                            config_packet = pending_config = payload
                            continue
                        if decoder is None:
                            continue
                        was_skipping = guard.skipping
                        decode = guard.decode(pts, key, time.monotonic())
                        lag_max = max(lag_max, guard.lag)
                        if not decode:
                            if not was_skipping:
                                self._event("phone_lag_skip", lag=round(guard.lag, 2),
                                            skips=guard.skips)
                                log.info("video %.1f s behind: skipping to a new keyframe",
                                         guard.lag)
                                self._send(reset_video_message())
                            continue
                        if was_skipping:
                            pending_config = config_packet
                        decoder.write(pending_config + payload if pending_config else payload)
                        pending_config = b""
                if decoder and time.monotonic() - stats_t >= 5:
                    fps = (frames - stats_frames) / (time.monotonic() - stats_t)
                    # lag_max: the worst delay behind the phone in these 5 s (Wi-Fi
                    # hiccups show up here without triggering a skip).
                    self._event("phone_fps", fps=round(fps, 1), frames=frames,
                                errors=decoder.errors, lag=round(guard.lag, 2),
                                lag_max=round(lag_max, 2), skips=guard.skips)
                    self._set_status(f"streaming {serial} {self._video_size[0]}x"
                                     f"{self._video_size[1]} {fps:.0f} fps")
                    stats_t, stats_frames, lag_max = time.monotonic(), frames, 0.0
        finally:
            if "status_stop" in locals():
                status_stop.set()
            with self._control_lock:
                self._control = None
            for sock in (video, control):
                if sock:
                    sock.close()
            if decoder:
                decoder.stop()
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
            self._procs.remove(server)
            self.adb.run("forward", "--remove", f"tcp:{port}", serial=serial, timeout=5)
            if log_file is not subprocess.DEVNULL:
                log_file.close()

    @staticmethod
    def _drain(sock: socket.socket) -> None:
        """Read and drop device → client messages so the socket never fills up."""
        try:
            while sock.recv(4096):
                pass
        except OSError:
            pass

    # ----- lifecycle -----

    def run(self) -> None:
        canvas_source = self.switch.canvas
        while not self._stop.is_set():
            serial = self._find_device()
            if not serial:
                self._stop.wait(3.0)
                continue
            self._event("phone_stream_start", serial=serial)
            try:
                self._stream(serial)
                reason = "stopped"
            except (PhoneDisconnected, OSError) as exc:
                reason = str(exc)
            self.switch.show(canvas_source)
            self._event("phone_stream_end", serial=serial, reason=reason)
            self._set_status(f"phone lost: {reason}")
            self._stop.wait(2.0)

    def stop(self) -> None:
        self._stop.set()
        for proc in list(self._procs):
            if proc.poll() is None:
                proc.terminate()
