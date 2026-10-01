"""Laptop-side helpers for phone mode (docs/phone-mode.md).

  pair-phone     pair the phone ("Wireless debugging → Pair device with pairing code")
                 with the adb key that prepare-sd.sh --phone copies onto the SD card,
                 so the Pi is trusted by the phone without ever needing a keyboard.
  phone-preview  run the phone pipeline on the laptop: scrcpy on the phone → decoded
                 frames → a plain VNC server on localhost. Open it with any VNC viewer
                 (touch works with the mouse) — the car-free way to try settings.
"""

from __future__ import annotations

import os
import pwd
import threading
import time
from pathlib import Path

from ..phone import Adb, PhoneLink, discover_adb_tls

ADB_SERVER_PORT = 5039          # our own adb server, never the user's normal one


def default_adb_home() -> Path:
    """~/.config/mlpi/adb of the real user (also under sudo)."""
    user = os.environ.get("SUDO_USER")
    home = Path(pwd.getpwnam(user).pw_dir) if user else Path.home()
    return home / ".config" / "mlpi" / "adb"


def ensure_key(adb_home: Path, adb_binary: str = "adb") -> Path:
    key = adb_home / ".android" / "adbkey"
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        Adb(adb_binary).run("keygen", str(key), timeout=60)
        if not key.exists():
            raise SystemExit(f"could not create an adb key at {key} (is adb installed?)")
        print(f"created the Pi's adb key: {key}")
    return key


def pair_phone(target: str = "", code: str = "", *, adb_home: Path | None = None,
               adb_binary: str = "adb") -> int:
    adb_home = adb_home or default_adb_home()
    ensure_key(adb_home, adb_binary)
    adb = Adb(adb_binary, home=str(adb_home), server_port=ADB_SERVER_PORT)
    if not target or not code:
        print("On the phone (connected to the same Wi-Fi as this laptop):\n"
              "  Settings → Developer options → Wireless debugging → ON\n"
              "  → 'Pair device with pairing code'. It shows an IP address:port and a code.")
        target = target or input("IP address:port shown on the phone: ").strip()
        code = code or input("pairing code: ").strip()
    out = adb.run("pair", target, code, timeout=60)
    print((out.stdout + out.stderr).strip())
    adb.run("kill-server", timeout=10)
    if out.returncode != 0 or "success" not in out.stdout.lower():
        print("pairing failed — the code is only valid while the pairing dialog is open")
        return 1
    print(f"\nPaired. The key in {adb_home}/.android is now trusted by the phone.\n"
          "Copy it to the SD card with:  sudo ./scripts/prepare-sd.sh --phone /dev/sdX")
    return 0


def phone_preview(*, serial: str = "", port: int = 5900, screenshot: Path | None = None,
                  server_jar: str = "", adb_home: Path | None = None, width: int = 800,
                  height: int = 480, start_app: str | None = None, dpi: int | None = None,
                  max_fps: int | None = None, bit_rate: int | None = None,
                  screen_off: bool = True,
                  adb_binary: str = "adb") -> int:
    from ..canvas import Canvas
    from ..config import Config
    from ..rfb import RfbServer
    from ..screen import StatusScreen
    from ..video import DisplaySwitch, InputRouter

    cfg = Config().phone
    repo = Path(__file__).resolve().parents[3]
    cfg.server_jar = server_jar or str(repo / "vendor" / "scrcpy-server")
    if not Path(cfg.server_jar).is_file():
        print(f"missing {cfg.server_jar}: run scripts/fetch-scrcpy-server.sh first")
        return 1
    if start_app is not None:
        cfg.start_app = start_app
    if dpi:
        cfg.dpi = dpi
    if max_fps:
        cfg.max_fps = max_fps
    if bit_rate:
        cfg.bit_rate = bit_rate
    cfg.screen_off = screen_off
    adb_home = adb_home or default_adb_home()
    adb = Adb(adb_binary, home=str(adb_home) if adb_home.exists() else "",
              server_port=ADB_SERVER_PORT)
    if serial and ":" in serial:
        adb.connect(serial)                      # wireless: pairing alone doesn't connect
    if not serial:
        devices = [s for s, state in adb.devices() if state == "device"]
        if not devices:
            # Paired phones announce their wireless-debugging port over mDNS.
            print("looking for the phone's Wireless debugging on this network …")
            for host, adb_port in discover_adb_tls("", "", timeout=4.0):
                target = f"{host}:{adb_port}"
                if adb.connect(target) and adb.state(target) == "device":
                    print(f"connected to {target}")
                    devices = [target]
                    break
        if not devices:
            print("no phone found. Check that Wireless debugging is ON and the phone is on the\n"
                  "same Wi-Fi, or pass --serial IP:PORT — the address on the Wireless debugging\n"
                  "screen itself (NOT the one in the pairing dialog).")
            adb.run("kill-server", timeout=10)
            return 1
        serial = devices[0]
    canvas = Canvas(width, height)
    switch = DisplaySwitch(canvas)
    link = PhoneLink(cfg, switch.new_video_frame(), switch, serial=serial, adb=adb)
    screen = StatusScreen(canvas, phone_status=lambda: link.status)
    router = InputRouter(screen, switch)
    router.attach_phone(link.frame, link)
    rfb = RfbServer(bind_address="127.0.0.1", port=port, canvas=switch, name="mlpi phone",
                    screen=router)
    for target in (link.run, rfb.serve_forever, screen.run):
        threading.Thread(target=target, daemon=True).start()
    print(f"phone {serial}: VNC server on 127.0.0.1:{port} — open it with a VNC viewer\n"
          "(e.g. `vncviewer 127.0.0.1::5900` or Remmina). Ctrl+C to stop.")
    shot_taken = False
    try:
        while True:
            time.sleep(1)
            if screenshot and not shot_taken and switch.showing(link.frame):
                time.sleep(1.5)
                screenshot.write_bytes(link.frame.to_png())
                print(f"saved {screenshot}")
                shot_taken = True
            print(f"\r{link.status[:70]:<70}", end="", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        link.stop()
        rfb.stop()
        screen.stop()
        adb.run("kill-server", timeout=10)
    return 0
