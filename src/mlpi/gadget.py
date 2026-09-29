"""USB gadget (configfs / libcomposite) setup, MirrorLink USB command, soft re-plug.

Creates one CDC-NCM (or ECM/RNDIS) network function, binds it to the Pi Zero's USB
device controller, and gives the resulting ``usb0`` its static address. Runs at boot
from ``mlpi-gadget.service`` (``python3 -m mlpi gadget-up``), which then stays running
as a small daemon (see ``run_daemon``). Idempotent.

MirrorLink USB command (ETSI TS 103 544-1 §4.2.2): the head unit sends the vendor
control request bmRequestType=0x40, bRequest=0xF0, wValue=<ML version>,
wIndex=<host VID> to detect a MirrorLink server; a STALL means "not a MirrorLink
server" (§4.2.3). A plain Linux gadget STALLs every vendor request. To accept it we
add a FunctionFS function (one vendor-specific interface) with the flags
FUNCTIONFS_ALL_CTRL_RECIP | FUNCTIONFS_CONFIG0_SETUP, which route device-recipient
control requests — also before SET_CONFIGURATION — to our process via ep0.

``soft_reconnect`` toggles the UDC's D+ pull-up. To the car that looks exactly like
unplugging and re-plugging the cable, which restarts its MirrorLink discovery — the
watchdog uses it when the car has gone quiet.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import struct
import subprocess
import time
from pathlib import Path

from .config import UsbConfig

log = logging.getLogger(__name__)

CONFIGFS = Path("/sys/kernel/config/usb_gadget")
UDC_CLASS = Path("/sys/class/udc")
GADGET_NAME = "g_mlpi"
FFS_NAME = "mlcmd"
FFS_MOUNT = Path("/dev/ffs-mlcmd")

# <linux/usb/functionfs.h>
FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_STRINGS_MAGIC = 2
FUNCTIONFS_HAS_FS_DESC = 1
FUNCTIONFS_HAS_HS_DESC = 2
FUNCTIONFS_ALL_CTRL_RECIP = 64
FUNCTIONFS_CONFIG0_SETUP = 128
FFS_EVENTS = {0: "BIND", 1: "UNBIND", 2: "ENABLE", 3: "DISABLE", 4: "SETUP",
              5: "SUSPEND", 6: "RESUME"}
FFS_EVENT_SIZE = 12   # struct usb_functionfs_event: usb_ctrlrequest (8) + type + 3 pad

ML_USB_REQUEST_TYPE = 0x40   # host-to-device, vendor, device recipient
ML_USB_REQUEST = 0xF0


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


def _umount_ffs() -> None:
    # Try a normal unmount, then a lazy one (a dead daemon can leave it "busy").
    for args in (["umount", str(FFS_MOUNT)], ["umount", "-l", str(FFS_MOUNT)]):
        if not os.path.ismount(FFS_MOUNT):
            return
        try:
            subprocess.run(args, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass


def _rmdir(path: Path) -> None:
    """rmdir that never raises — configfs dirs must come down in order and a single
    stuck entry (e.g. a still-mounted ffs function) must not abort the whole teardown."""
    try:
        path.rmdir()
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def teardown(gadget: Path) -> None:
    """Remove a configfs gadget tree (reverse order of creation). Never raises: a
    leftover from a crashed run must not turn into a restart loop."""
    if not gadget.exists():
        return
    try:
        _write(gadget / "UDC", "")
    except OSError:
        pass
    _umount_ffs()
    for cfg in (gadget / "configs").glob("*"):
        for link in cfg.iterdir():
            if link.is_symlink():
                try:
                    link.unlink()
                except OSError as exc:
                    log.warning("could not unlink %s: %s", link, exc)
        for strings in (cfg / "strings").glob("*"):
            _rmdir(strings)
        _rmdir(cfg)
    for func in (gadget / "functions").glob("*"):
        _rmdir(func)
    for strings in (gadget / "strings").glob("*"):
        _rmdir(strings)
    _rmdir(gadget)


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


# ---------- FunctionFS function for the MirrorLink USB command ----------

def ffs_descriptors() -> bytes:
    """FunctionFS v2 descriptor blob: one vendor-specific interface, 2 bulk endpoints
    (FunctionFS needs endpoints to bind reliably; we never open them)."""
    intf = struct.pack("<9B", 9, 4, 0, 0, 2, 0xFF, 0, 0, 1)   # iInterface = string 1

    def ep(address: int, max_packet: int) -> bytes:
        return struct.pack("<BBBBHB", 7, 5, address, 2, max_packet, 0)

    full_speed = intf + ep(0x81, 64) + ep(0x02, 64)
    high_speed = intf + ep(0x81, 512) + ep(0x02, 512)
    flags = (FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
             | FUNCTIONFS_ALL_CTRL_RECIP | FUNCTIONFS_CONFIG0_SETUP)
    body = struct.pack("<II", 3, 3) + full_speed + high_speed
    return struct.pack("<III", FUNCTIONFS_DESCRIPTORS_MAGIC_V2, 12 + len(body), flags) + body


def ffs_strings(name: str = "MirrorLink") -> bytes:
    body = struct.pack("<H", 0x0409) + name.encode("ascii") + b"\0"
    return struct.pack("<IIII", FUNCTIONFS_STRINGS_MAGIC, 16 + len(body), 1, 1) + body


def parse_ffs_events(data: bytes) -> list[tuple[str, dict[str, int] | None]]:
    """Split an ep0 read into (event name, setup-request fields or None)."""
    out = []
    for off in range(0, len(data) - FFS_EVENT_SIZE + 1, FFS_EVENT_SIZE):
        chunk = data[off:off + FFS_EVENT_SIZE]
        etype = chunk[8]
        name = FFS_EVENTS.get(etype, f"type{etype}")
        setup = None
        if name == "SETUP":
            rt, req, value, index, length = struct.unpack_from("<BBHHH", chunk)
            setup = {"bmRequestType": rt, "bRequest": req, "wValue": value,
                     "wIndex": index, "wLength": length}
        out.append((name, setup))
    return out


def is_ml_command(setup: dict[str, int]) -> bool:
    return (setup["bmRequestType"] == ML_USB_REQUEST_TYPE
            and setup["bRequest"] == ML_USB_REQUEST)


def ml_version_from_wvalue(wvalue: int) -> str:
    """wValue: low byte = major, high byte = minor (§4.2.2); 0.1 counts as 1.0."""
    major, minor = wvalue & 0xFF, wvalue >> 8
    if (major, minor) == (0, 1):
        return "1.0"
    return f"{major}.{minor}"


def _setup_ffs(g: Path) -> int:
    """Add the FunctionFS function to config c.1; returns the ep0 fd (keep it open!)."""
    fdir = g / "functions" / f"ffs.{FFS_NAME}"
    fdir.mkdir()
    (g / "configs/c.1" / f"ffs.{FFS_NAME}").symlink_to(fdir)
    FFS_MOUNT.mkdir(parents=True, exist_ok=True)
    _umount_ffs()
    subprocess.run(["mount", "-t", "functionfs", FFS_NAME, str(FFS_MOUNT)], check=True,
                   capture_output=True, timeout=20)
    fd = os.open(FFS_MOUNT / "ep0", os.O_RDWR)
    try:
        os.write(fd, ffs_descriptors())
        os.write(fd, ffs_strings())
    except OSError:
        os.close(fd)
        raise
    return fd


def _remove_ffs(g: Path, fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    link = g / "configs/c.1" / f"ffs.{FFS_NAME}"
    if link.is_symlink():
        link.unlink()
    _umount_ffs()
    fdir = g / "functions" / f"ffs.{FFS_NAME}"
    if fdir.exists():
        try:
            fdir.rmdir()
        except OSError as exc:
            log.warning("could not remove %s: %s", fdir, exc)


def gadget_up(usb: UsbConfig, *, wait_udc: float = 30.0) -> dict[str, str]:
    """Create and bind the gadget, configure usb0. Returns facts for the session log.

    With ``usb.ml_command`` the FunctionFS function is added; its ep0 fd is returned
    in ``facts["ep0_fd"]`` and must stay open for the gadget to stay bound.
    """
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
    if g.exists():
        # A previous run left a gadget teardown couldn't fully remove (usually a
        # still-mounted FunctionFS). Reboot clears configfs; say so instead of looping.
        raise GadgetError(
            f"{g} still present after teardown — a previous gadget is stuck "
            "(often a busy FunctionFS mount). Reboot to clear configfs, and set "
            "'[usb] ml_command = false' in mlpi.toml if it recurs.")

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

    ep0_fd: int | None = None
    ml_command = "disabled"
    if usb.ml_command:
        try:
            ep0_fd = _setup_ffs(g)
            ml_command = "enabled"
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("MirrorLink USB command support unavailable (%s); NCM only", exc)
            _remove_ffs(g, ep0_fd)
            ep0_fd = None
            ml_command = f"failed: {exc}"
    try:
        _write(g / "UDC", udc)
    except OSError as exc:
        if ep0_fd is None:
            raise
        log.warning("binding with FunctionFS failed (%s); retrying NCM only", exc)
        _remove_ffs(g, ep0_fd)
        ep0_fd = None
        ml_command = f"bind failed: {exc}"
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
             "ifname": ifname, "address": usb.address, "released": ",".join(released),
             "ml_command": ml_command, "ep0_fd": ep0_fd}
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


# ---------- daemon ----------

def sd_notify(message: str) -> None:
    """Minimal sd_notify(3) for Type=notify units (no-op outside systemd)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as exc:
        log.warning("sd_notify failed: %s", exc)


class UsbLog:
    """Appends USB-level events to <session>/usb.jsonl (separate from the main
    service's events.jsonl, which another process owns)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._t0 = time.monotonic()

    def __call__(self, kind: str, **fields) -> None:
        record = {"t": round(time.monotonic() - self._t0, 3), "wall": time.time(),
                  "kind": kind, **fields}
        log.info("usb %s %s", kind, fields)
        if self.path is None:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.warning("usb log write failed: %s", exc)


def _answer_setup(fd: int, setup: dict[str, int]) -> bool:
    """ACK the MirrorLink USB command, STALL everything else. Returns True if ACKed.

    FunctionFS: for a host-to-device request, read(ep0, wLength) completes the data
    and status stages; doing the opposite-direction operation stalls (§4.2.3 wants a
    STALL for requests we don't support)."""
    host_to_device = not setup["bmRequestType"] & 0x80
    try:
        if is_ml_command(setup):
            os.read(fd, setup["wLength"])
            return True
        if host_to_device:
            os.write(fd, b"")      # wrong direction → STALL
        else:
            os.read(fd, 0)         # wrong direction → STALL
    except OSError:
        pass                       # a STALL is reported as an error (EL2HLT)
    return False


def _rx_packets(ifname: str) -> int:
    try:
        return int((Path("/sys/class/net") / ifname / "statistics/rx_packets").read_text())
    except (OSError, ValueError):
        return 0


def run_daemon(usb: UsbConfig, session_dir: Path | None, *,
               configure_timeout: float = 15.0, traffic_timeout: float = 40.0,
               restart_units: tuple[str, ...] = ("mlpi.service", "mlpi-capture.service")
               ) -> None:
    """Bring the gadget up, report READY to systemd, then keep ep0 open forever."""
    usb_log = UsbLog(session_dir / "usb.jsonl" if session_dir else None)
    facts = gadget_up(usb)
    fd: int | None = facts.pop("ep0_fd")  # type: ignore[assignment]
    usb_log("gadget_up", **facts)
    if session_dir is not None:
        try:
            (session_dir / "gadget.json").write_text(json.dumps(facts, indent=2) + "\n")
        except OSError:
            pass
    sd_notify("READY=1")

    udc, ifname = facts["udc"], facts["ifname"]
    g = CONFIGFS / GADGET_NAME
    last_state = ""
    host_seen_at: float | None = None      # entered default/addressed
    configured_at: float | None = None
    rx_at_configure = 0
    commands = 0
    while True:
        # 1) FunctionFS events (only while the extra function is present)
        if fd is not None:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                try:
                    data = os.read(fd, FFS_EVENT_SIZE * 4)
                except OSError as exc:
                    usb_log("ffs_read_error", error=str(exc))
                    time.sleep(1.0)
                    data = b""
                for name, setup in parse_ffs_events(data):
                    if setup is None:
                        usb_log("ffs_event", event=name)
                        continue
                    acked = _answer_setup(fd, setup)
                    if acked:
                        commands += 1
                        usb_log("ml_usb_command", ml_version=ml_version_from_wvalue(
                            setup["wValue"]), host_vid=f"0x{setup['wIndex']:04x}",
                            count=commands, **setup)
                    else:
                        usb_log("ffs_setup_stalled", **setup)
        else:
            time.sleep(1.0)

        # 2) UDC state (what the host did with us)
        state = udc_state(udc)
        now = time.monotonic()
        if state != last_state:
            usb_log("udc_state", state=state, previous=last_state)
            last_state = state
            # Only a bus reset means an active host. "powered" is just VBUS: in the
            # car the head unit may still be booting for tens of seconds.
            if state in ("default", "addressed"):
                host_seen_at = host_seen_at or now
            elif state == "configured":
                configured_at = now
                rx_at_configure = _rx_packets(ifname)
            elif state == "not attached":
                host_seen_at = configured_at = None

        # 3) Fallback: if the extra interface seems to put the host off, drop it.
        if fd is None:
            continue
        reason = ""
        if host_seen_at and not configured_at and now - host_seen_at > configure_timeout:
            reason = f"host did not configure the device within {configure_timeout:.0f}s"
        elif (configured_at and now - configured_at > traffic_timeout
              and _rx_packets(ifname) <= rx_at_configure and commands == 0):
            reason = f"no network traffic from the host within {traffic_timeout:.0f}s"
        if reason:
            usb_log("ffs_fallback", reason=reason)
            fd = _drop_ffs(g, udc, fd, ifname, usb, usb_log, restart_units)
            host_seen_at = configured_at = None


def _drop_ffs(g: Path, udc: str, fd: int, ifname: str, usb: UsbConfig, usb_log: UsbLog,
              restart_units: tuple[str, ...]) -> int | None:
    """Rebind without the FunctionFS function; keep usb0 (and its address) if we can."""
    index_before = _ifindex(ifname)
    try:
        _write(g / "UDC", "")
    except OSError:
        pass
    _remove_ffs(g, fd)
    _write(g / "UDC", udc)
    time.sleep(1.0)
    if not Path(f"/sys/class/net/{ifname}").exists() or _ifindex(ifname) != index_before:
        subprocess.run(["ip", "addr", "replace", usb.address, "dev", ifname], check=False)
        subprocess.run(["ip", "link", "set", ifname, "up"], check=False)
        subprocess.run(["systemctl", "restart", *restart_units], check=False)
        usb_log("ffs_fallback_done", restarted=list(restart_units))
    else:
        usb_log("ffs_fallback_done", restarted=[])
    return None


def _ifindex(ifname: str) -> int:
    try:
        return int((Path("/sys/class/net") / ifname / "ifindex").read_text())
    except (OSError, ValueError):
        return -1
