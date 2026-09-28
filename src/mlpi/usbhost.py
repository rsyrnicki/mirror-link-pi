"""Host-side USB: send the MirrorLink USB command to an attached device.

This is the *client* (car) side of the command that ``gadget.py`` answers on the
Pi. It runs on a Linux laptop acting as USB host, to wake a real MirrorLink phone
into MirrorLink mode (ETSI TS 103 544-1 §4.2.2). It uses the kernel's usbfs
(``/dev/bus/usb/BBB/DDD``) USBDEVFS_CONTROL ioctl, so no third-party library is
needed.

The request (§4.2.2):
    bmRequestType = 0x40 (host→device, vendor, device recipient)
    bRequest      = 0xF0
    wValue        = MirrorLink version (low byte major, high byte minor)
    wIndex        = USB host vendor ID
    wLength       = 0
A STALL means "this device is not (currently) a MirrorLink server" (§4.2.3).
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

USB_DIR = Path("/sys/bus/usb/devices")
ML_REQUEST = 0xF0
ML_REQUEST_TYPE = 0x40

ML_VERSIONS = {"1.0": 0x0001, "1.1": 0x0101, "1.2": 0x0201, "1.3": 0x0301}

# Known MirrorLink-capable vendor IDs (for --auto device discovery).
KNOWN_VENDORS = {0x04E8: "Samsung", 0x0BB4: "HTC", 0x1004: "LG", 0x054C: "Sony",
                 0x22B8: "Motorola", 0x18D1: "Google", 0x2916: "Yota"}

# struct usbdevfs_ctrltransfer { u8 bRequestType, bRequest; u16 wValue, wIndex,
#   wLength; u32 timeout; void *data; }  — on 64-bit: 8 bytes header + 4 pad + ptr.
_CTRL_FMT = "BBHHHIQ"  # last field: pointer as u64 (x86-64 / arm64)
_USBDEVFS_CONTROL = 0xC0185500  # _IOWR('U', 0, struct usbdevfs_ctrltransfer), 64-bit


@dataclass
class UsbDevice:
    bus: int
    address: int
    vendor: int
    product: int
    manufacturer: str
    product_name: str
    sysfs: Path

    @property
    def node(self) -> Path:
        return Path(f"/dev/bus/usb/{self.bus:03d}/{self.address:03d}")

    def describe(self) -> str:
        vendor = KNOWN_VENDORS.get(self.vendor, "")
        tag = f" ({vendor})" if vendor else ""
        return (f"{self.vendor:04x}:{self.product:04x}{tag} "
                f"{self.manufacturer} {self.product_name} @ bus {self.bus} dev {self.address}")


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def list_devices() -> list[UsbDevice]:
    out = []
    for entry in sorted(USB_DIR.glob("*")):
        vid, pid = _read(entry / "idVendor"), _read(entry / "idProduct")
        busnum, devnum = _read(entry / "busnum"), _read(entry / "devnum")
        if not (vid and pid and busnum and devnum):
            continue
        try:
            out.append(UsbDevice(
                bus=int(busnum), address=int(devnum), vendor=int(vid, 16),
                product=int(pid, 16), manufacturer=_read(entry / "manufacturer"),
                product_name=_read(entry / "product"), sysfs=entry))
        except ValueError:
            continue
    return out


def find_phone(vendor: int | None = None) -> UsbDevice | None:
    devices = list_devices()
    if vendor is not None:
        matches = [d for d in devices if d.vendor == vendor]
    else:
        matches = [d for d in devices if d.vendor in KNOWN_VENDORS]
    return matches[0] if matches else None


def send_ml_command(device: UsbDevice, *, version: str = "1.1", host_vid: int = 0x1D6B,
                    timeout_ms: int = 1000) -> dict[str, object]:
    """Send the MirrorLink USB command. Returns a result dict (never raises for a
    STALL — a STALL is a valid, meaningful answer)."""
    wvalue = ML_VERSIONS.get(version)
    if wvalue is None:
        raise ValueError(f"unknown MirrorLink version {version!r}; know {list(ML_VERSIONS)}")
    buf = ctypes.create_string_buffer(0)
    ctrl = struct.pack(_CTRL_FMT, ML_REQUEST_TYPE, ML_REQUEST, wvalue, host_vid, 0,
                       timeout_ms, ctypes.addressof(buf))
    mutable = bytearray(ctrl)
    result: dict[str, object] = {"device": device.describe(), "version": version,
                                 "host_vid": f"0x{host_vid:04x}"}
    try:
        with open(device.node, "wb") as fh:
            fcntl.ioctl(fh.fileno(), _USBDEVFS_CONTROL, mutable, True)
        result["outcome"] = "acked"
        log.info("MirrorLink USB command ACKed by %s", device.describe())
    except OSError as exc:
        # EPIPE (32) is the STALL; the device declined MirrorLink mode.
        result["outcome"] = "stalled" if exc.errno == 32 else f"error:{exc.errno}"
        result["error"] = str(exc)
        log.info("MirrorLink USB command → %s: %s", result["outcome"], exc)
    return result
