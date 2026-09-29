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

import logging
import os
import random
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

CODEC_H264 = 0x68323634

# Control message types (scrcpy app/src/control_msg.h, enum order).
MSG_INJECT_KEYCODE = 0
MSG_INJECT_TOUCH_EVENT = 2
MSG_BACK_OR_SCREEN_ON = 4
MSG_SET_DISPLAY_POWER = 10
MSG_START_APP = 16

ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_ID_GENERIC_FINGER = (1 << 64) - 2           # UINT64_C(-2)

KEYCODE_HOME, KEYCODE_BACK, KEYCODE_DPAD_CENTER = 3, 4, 23

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


def back_or_screen_on_message(action: int) -> bytes:
    return struct.pack("!BB", MSG_BACK_OR_SCREEN_ON, action)


def display_power_message(on: bool) -> bytes:
    """With a virtual display, this powers the phone's *own* screen (scrcpy
    Controller.setDisplayPower); apps keep rendering on the virtual display."""
    return struct.pack("!BB", MSG_SET_DISPLAY_POWER, int(on))


def start_app_message(name: str) -> bytes:
    raw = name.encode()[:255]
    return struct.pack("!BB", MSG_START_APP, len(raw)) + raw


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
                 codec: str = "", threads: int = 1) -> None:
        from .avdecode import AvDecoder
        self.av = AvDecoder(width, height, codec=codec or "h264", threads=threads)
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


def discover_adb_tls(interface: str, local_ip: str, *,
                     timeout: float = 2.0) -> list[tuple[str, int]]:
    """Ask the Wi-Fi network for wireless-debugging endpoints: [(ip, port), ...]."""
    found: list[tuple[str, int]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        if interface:
            sock.setsockopt(socket.SOL_SOCKET, 25, interface.encode())   # SO_BINDTODEVICE
        if local_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.5)
        query = mdns_query()
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
            for _instance, (target, port) in answer["srv"].items():
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
        self._down = False
        self._last_pos = (-1, -1)
        self._procs: list[subprocess.Popen] = []

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

    def on_pointer(self, x: int, y: int, buttons: int) -> None:
        vw, vh = self._video_size
        fx = min(vw - 1, max(0, x * vw // self.frame.width))
        fy = min(vh - 1, max(0, y * vh // self.frame.height))
        pressed = bool(buttons & 1)
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
        keycode = KEYMAP.get(keysym)
        if keycode is None:
            self._event("phone_key_unmapped", keysym=f"0x{keysym:08x}", down=down)
            return
        self._send(keycode_message(ACTION_DOWN if down else ACTION_UP, keycode))

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
            self._set_status(f"looking for wireless debugging on {ip}")
            targets += [f"{h}:{p}" for h, p in discover_adb_tls(
                self.cfg.interface, self.local_ip) if h == ip]
            for target in targets:
                if not self.adb.connect(target):
                    continue
                state = self.adb.state(target)
                self._event("phone_adb_connect", target=target, state=state)
                if state == "device":
                    return target
                if state == "unauthorized":
                    self._set_status("phone refused adb: pair it (docs/phone-mode.md)")
        if self.status.startswith("looking"):
            self._set_status("phone on Wi-Fi, but wireless debugging is off")
        return ""

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
        video = control = None
        try:
            video = self._connect_video(port, time.monotonic() + 20)
            control = socket.create_connection(("127.0.0.1", port), timeout=5)
            with self._control_lock:
                self._control = control
            threading.Thread(target=self._drain, args=(control,), daemon=True).start()
            if c.start_app:
                self._send(start_app_message(c.start_app))
            if getattr(c, "screen_off", False):
                # Locking the phone blanks the virtual display; a dark (but unlocked)
                # screen doesn't, and saves battery.
                self._send(display_power_message(False))
            video.settimeout(10)
            parser = StreamParser(dummy_byte=False)
            config_packet = b""        # SPS/PPS: kept for decoder restarts
            pending_config = b""       # merged into the next frame, as scrcpy's client does
            first = True
            stats_t, stats_frames = time.monotonic(), 0

            def on_frame(data: bytes) -> None:
                nonlocal first
                self.frame.update(data)
                if first:
                    first = False
                    self.switch.show(self.frame)
                    self._event("phone_first_frame")

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
                        self._event("phone_device", name=ev[1])
                    elif kind == "codec" and ev[1] != CODEC_H264:
                        raise PhoneDisconnected(f"unexpected codec 0x{ev[1]:08x}")
                    elif kind == "session":
                        _, w, h, _resized = ev
                        self._video_size = (w, h)
                        self._event("phone_session", width=w, height=h)
                        self._set_status(f"streaming {serial} {w}x{h}")
                        if decoder:
                            decoder.stop()
                        try:
                            decoder = Decoder(self.frame.width, self.frame.height, on_frame,
                                              codec=c.decoder, threads=c.decoder_threads)
                        except Exception as exc:  # noqa: BLE001 - e.g. libavcodec missing
                            raise PhoneDisconnected(f"decoder unavailable: {exc}") from None
                        pending_config = config_packet
                    elif kind == "packet":
                        _, is_config, _key, _pts, payload = ev
                        if is_config:
                            config_packet = pending_config = payload
                            continue
                        if decoder is None:
                            continue
                        decoder.write(pending_config + payload if pending_config else payload)
                        pending_config = b""
                if decoder and time.monotonic() - stats_t >= 5:
                    fps = (decoder.frames - stats_frames) / (time.monotonic() - stats_t)
                    self._event("phone_fps", fps=round(fps, 1), frames=decoder.frames,
                                errors=decoder.errors)
                    self._set_status(f"streaming {serial} {self._video_size[0]}x"
                                     f"{self._video_size[1]} {fps:.0f} fps")
                    stats_t, stats_frames = time.monotonic(), decoder.frames
        finally:
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
