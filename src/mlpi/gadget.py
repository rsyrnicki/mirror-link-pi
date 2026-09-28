"""USB gadget (configfs / libcomposite) setup and soft re-plug.

Creates one CDC-NCM (or ECM/RNDIS) network function, binds it to the Pi Zero's USB
device controller, and gives the resulting ``usb0`` its static address. Runs at boot
from ``mlpi-gadget.service`` (``python3 -m mlpi gadget-up``). Idempotent.

``soft_reconnect`` toggles the UDC's D+ pull-up. To the car that looks exactly like
unplugging and re-plugging the cable, which restarts its MirrorLink discovery — the
watchdog uses it when the car has gone quiet.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from .config import UsbConfig

log = logging.getLogger(__name__)

CONFIGFS = Path("/sys/kernel/config/usb_gadget")
UDC_CLASS = Path("/sys/class/udc")
GADGET_NAME = "g_mlpi"


class GadgetError(RuntimeError):
    pass


def _write(path: Path, value: str) -> None:
    path.write_text(value + "\n")


def list_udcs() -> list[str]:
    try:
        return sorted(p.name for p in UDC_CLASS.iterdir())
    except FileNotFoundError:
        return []


def _modprobe(module: str, *, remove: bool = False) -> None:
    argv = ["modprobe", "-r", module] if remove else ["modprobe", module]
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("%s failed: %s", " ".join(argv), exc)


def teardown(gadget: Path) -> None:
    """Remove a configfs gadget tree (reverse order of creation)."""
    if not gadget.exists():
        return
    try:
        _write(gadget / "UDC", "")
    except OSError:
        pass
    for cfg in (gadget / "configs").glob("*"):
        for link in cfg.iterdir():
            if link.is_symlink():
                link.unlink()
        for strings in (cfg / "strings").glob("*"):
            strings.rmdir()
        cfg.rmdir()
    for func in (gadget / "functions").glob("*"):
        func.rmdir()
    for strings in (gadget / "strings").glob("*"):
        strings.rmdir()
    gadget.rmdir()


def release_foreign_gadgets() -> list[str]:
    """Unbind other gadgets that hold the UDC (e.g. Pi OS 'USB gadget mode', g_ether)."""
    released = []
    for other in CONFIGFS.glob("*"):
        if other.name == GADGET_NAME:
            continue
        try:
            if (other / "UDC").read_text().strip():
                _write(other / "UDC", "")
                released.append(other.name)
        except OSError:
            continue
    for module in ("g_ether", "g_multi", "g_serial", "g_mass_storage"):
        if Path(f"/sys/module/{module}").exists():
            _modprobe(module, remove=True)
            released.append(module)
    if released:
        log.warning("released USB controller from: %s", ", ".join(released))
    return released


def gadget_up(usb: UsbConfig, *, wait_udc: float = 30.0) -> dict[str, str]:
    """Create and bind the gadget, configure usb0. Returns facts for the session log."""
    if os.geteuid() != 0:
        raise GadgetError("must run as root")
    _modprobe("libcomposite")
    if not CONFIGFS.is_dir():
        raise GadgetError(f"configfs not mounted at {CONFIGFS}")

    deadline = time.monotonic() + wait_udc
    while not list_udcs():
        if time.monotonic() > deadline:
            raise GadgetError(
                "no USB device controller — is 'dtoverlay=dwc2,dr_mode=peripheral' in "
                "/boot/firmware/config.txt and is the cable in the Pi's USB (not PWR) port?")
        time.sleep(0.5)
    udc = list_udcs()[0]

    released = release_foreign_gadgets()
    g = CONFIGFS / GADGET_NAME
    teardown(g)

    func = usb.function
    if func not in ("ncm", "ecm", "rndis"):
        raise GadgetError(f"usb.function must be ncm, ecm or rndis (got {func!r})")

    g.mkdir()
    _write(g / "idVendor", usb.vid)
    _write(g / "idProduct", usb.pid)
    _write(g / "bcdDevice", "0x0100")
    _write(g / "bcdUSB", "0x0200")
    (g / "strings/0x409").mkdir(parents=True)
    _write(g / "strings/0x409/serialnumber", usb.serial)
    _write(g / "strings/0x409/manufacturer", usb.manufacturer)
    _write(g / "strings/0x409/product", usb.product)
    (g / "configs/c.1/strings/0x409").mkdir(parents=True)
    _write(g / "configs/c.1/strings/0x409/configuration", "MLPI Config")
    _write(g / "configs/c.1/MaxPower", "250")
    fdir = g / "functions" / f"{func}.usb0"
    fdir.mkdir(parents=True)
    _write(fdir / "host_addr", usb.host_mac)
    _write(fdir / "dev_addr", usb.dev_mac)
    (g / "configs/c.1" / f"{func}.usb0").symlink_to(fdir)
    _write(g / "UDC", udc)

    ifname = "usb0"
    try:
        ifname = (fdir / "ifname").read_text().strip() or ifname
    except OSError:
        pass
    for _ in range(40):
        if Path(f"/sys/class/net/{ifname}").exists():
            break
        time.sleep(0.25)
    else:
        raise GadgetError(f"gadget bound to {udc} but {ifname} did not appear")

    subprocess.run(["ip", "addr", "flush", "dev", ifname], check=True)
    subprocess.run(["ip", "addr", "add", usb.address, "dev", ifname], check=True)
    subprocess.run(["ip", "link", "set", ifname, "up"], check=True)
    facts = {"udc": udc, "function": func, "vid": usb.vid, "pid": usb.pid,
             "ifname": ifname, "address": usb.address, "released": ",".join(released)}
    log.info("gadget up: %s", facts)
    return facts


def udc_state(udc: str | None = None) -> str:
    udc = udc or (list_udcs() or [""])[0]
    try:
        return (UDC_CLASS / udc / "state").read_text().strip()
    except OSError:
        return "unknown"


def soft_reconnect(udc: str | None = None, *, off_seconds: float = 2.0) -> bool:
    """Simulate unplug + replug by toggling the D+ pull-up. Returns True on success."""
    udc = udc or (list_udcs() or [""])[0]
    path = UDC_CLASS / udc / "soft_connect"
    try:
        _write(path, "disconnect")
        time.sleep(off_seconds)
        _write(path, "connect")
        return True
    except OSError as exc:
        log.warning("soft reconnect via %s failed: %s", path, exc)
        return False


def carrier(ifname: str) -> bool | None:
    try:
        return (Path("/sys/class/net") / ifname / "carrier").read_text().strip() == "1"
    except OSError:
        return None
