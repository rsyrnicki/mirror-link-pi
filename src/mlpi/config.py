"""Configuration loading for MirrorLink-Pi.

Resolution order (later overrides earlier):
  1. Built-in defaults
  2. /etc/mlpi/mlpi.toml          (system)
  3. /boot/firmware/mlpi.toml     (boot partition — editable from any laptop OS)
  4. ./config/mlpi.toml           (project working dir)
  5. Path passed to ``load(path=...)``
  6. Environment variables (MLPI_*)
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class NetworkConfig:
    interface: str = "usb0"
    address: str = ""           # empty = autodetect from `interface`
    http_port: int = 8080
    # Our own RFB server (mlpi.rfb) listens here; LaunchApplication hands it out.
    vnc_port: int = 5900
    # Device Attestation Protocol stub (only advertised by variants with dap = true).
    dap_port: int = 5510


@dataclass
class UsbConfig:
    """USB gadget (configfs) settings, applied by ``mlpi gadget-up`` at boot."""
    function: str = "ncm"                # ncm | ecm | rndis
    vid: str = "0x1d6b"                  # Linux Foundation
    pid: str = "0x0104"                  # Multifunction Composite Gadget
    manufacturer: str = "MirrorLink Pi"
    product: str = "MLPI Gadget"
    serial: str = "0123456789"
    host_mac: str = "02:1a:11:00:00:01"  # MAC of the car-side network interface
    dev_mac: str = "02:1a:11:00:00:02"   # MAC of usb0 on the Pi
    address: str = "192.168.7.2/24"      # static IPv4 of usb0
    # Answer the MirrorLink USB command (Part 1 §4.2.2) via a FunctionFS interface
    # instead of STALLing it. OFF by default: it is the newest, most hardware-fragile
    # path (composite gadget + FunctionFS). The plain NCM gadget is proven. Turn it on
    # (and reboot) once the NCM gadget is confirmed working on your Pi.
    ml_command: bool = False


@dataclass
class DhcpConfig:
    enabled: bool = True
    # The car got .44 from dnsmasq in every session so far; keep it stable so old
    # probe scripts (CAR_IP=192.168.7.44) keep working.
    client_address: str = "192.168.7.44"
    lease_seconds: int = 3600
    offer_router: bool = True
    offer_dns: bool = True


@dataclass
class VncConfig:
    enabled: bool = True
    width: int = 800
    height: int = 480
    name: str = "MirrorLink Pi"
    # Bytes of raw client→server traffic kept per connection (for later analysis).
    raw_dump_limit: int = 1_048_576


@dataclass
class SsdpConfig:
    multicast_group: str = "239.255.255.250"
    multicast_port: int = 1900
    multicast_ttl: int = 2
    notify_interval_seconds: int = 300
    max_age_seconds: int = 1800
    device_uuid: str = "c8cba096-5abe-47ac-9c14-3267d7c94ce6"


@dataclass
class DeviceConfig:
    friendly_name: str = "MirrorLink Pi"
    manufacturer: str = "Sirnicki & Maksymilian"
    model_name: str = "mlpi"
    model_number: str = "0.2"
    device_type: str = "urn:schemas-upnp-org:device:TmServerDevice:1"


@dataclass
class ExperimentConfig:
    # "rotate": switch to the next variant on every new connection attempt by the
    #           car, until one of them makes the car open the VNC connection.
    # "fixed":  always use `fixed_variant`.
    mode: str = "rotate"
    fixed_variant: str = "spec-1.0"
    # Empty = config/variants.toml shipped in the repo.
    variants_file: str = ""
    # A descriptor fetch starts a new attempt once the previous attempt got its app list,
    # or after this many seconds of silence (the MIB2 loops every ~2.7 s, no pause).
    attempt_gap_seconds: float = 4.0
    # Attempts (handshake cycles) each variant gets before rotating to the next.
    cycles_per_variant: int = 2


@dataclass
class WatchdogConfig:
    # If the USB link is up but the car has been silent this long (and no VNC client
    # is connected), simulate an unplug/replug via the UDC soft_connect switch. The
    # car normally retries by itself every ~10 s, so this only fires when it gave up.
    # 0 disables the watchdog.
    idle_reconnect_seconds: int = 90
    max_reconnects: int = 20


def _default_apps() -> list[dict]:
    from .launcher import DEFAULT_APPS
    return [{"name": a.name, "package": a.package, "colour": a.colour} for a in DEFAULT_APPS]


@dataclass
class PhoneConfig:
    """Phone mode: mirror an Android phone over Wi-Fi with scrcpy (docs/phone-mode.md)."""
    enabled: bool = False
    # The Pi's own Wi-Fi hotspot the phone joins. No internet is offered on it, so the
    # phone keeps using mobile data for Maps/Spotify.
    interface: str = "wlan0"
    address: str = "192.168.8.1/24"
    client_address: str = "192.168.8.44"
    manage_hotspot: bool = True
    wifi_ssid: str = "MirrorLink-Pi"
    wifi_password: str = ""              # 8-63 characters; prepare-sd.sh --phone sets one
    wifi_country: str = "DE"
    wifi_channel: int = 6
    # adb: its key (paired once with `mlpi pair-phone` on the laptop) lives in adb_home.
    adb: str = "adb"
    adb_home: str = "/var/lib/mlpi/adb"
    legacy_port: int = 5555              # also try `adb tcpip` mode; 0 = don't
    server_jar: str = "/opt/mlpi/vendor/scrcpy-server"
    # Video: a new virtual display on the phone, exactly the car's screen size.
    # 120 dpi makes 800x480 px count as a 1067x640 dp "tablet": apps then offer their
    # landscape layouts (at 200 dpi, portrait-only apps like Audible showed pillarboxed).
    dpi: int = 120
    max_fps: int = 30
    bit_rate: int = 4_000_000
    # The Pi draws its own launcher (tiles for `apps`, a Home button over the video).
    # With start_app set, that app opens directly instead.
    launcher: bool = True
    # Home-page tiles (up to 7; an "All apps" tile is added). Override in mlpi.toml:
    #   apps = [{name = "Maps", package = "com.google.android.apps.maps", colour = "#1a73e8"}]
    apps: list = field(default_factory=lambda: _default_apps())
    start_app: str = ""
    system_decorations: bool = False     # the phone's own launcher + nav bar on the display
    keep_active: bool = True             # keep the phone awake while mirroring
    screen_off: bool = True              # phone's own screen off (not locked) meanwhile
    decoder: str = ""                    # "" = software h264; "h264_v4l2m2m" = Pi hardware
    decoder_threads: int = 1
    max_lag: float = 2.0                 # seconds behind the phone before skipping ahead
    avoid_bad_wifi: bool = True          # set Android's "avoid bad Wi-Fi" so mobile data
                                         # stays the phone's internet


@dataclass
class SessionConfig:
    # Every boot gets its own directory under <root>/sessions/.
    root: str = "/var/lib/mlpi"


@dataclass
class LedConfig:
    enabled: bool = True
    path: str = ""              # empty = autodetect (/sys/class/leds/ACT or led0)


@dataclass
class LoggingConfig:
    file: str = ""              # empty = session dir, else autodetect
    level: str = "DEBUG"


@dataclass
class Config:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    usb: UsbConfig = field(default_factory=UsbConfig)
    dhcp: DhcpConfig = field(default_factory=DhcpConfig)
    vnc: VncConfig = field(default_factory=VncConfig)
    ssdp: SsdpConfig = field(default_factory=SsdpConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    experiment: ExperimentConfig = field(default_factory=ExperimentConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    phone: PhoneConfig = field(default_factory=PhoneConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    led: LedConfig = field(default_factory=LedConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


# Map env-var name → (section, field). Only declared keys are honored; typos are silently ignored.
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "MLPI_INTERFACE": ("network", "interface"),
    "MLPI_ADDRESS": ("network", "address"),
    "MLPI_HTTP_PORT": ("network", "http_port"),
    "MLPI_VNC_PORT": ("network", "vnc_port"),
    "MLPI_DEVICE_UUID": ("ssdp", "device_uuid"),
    "MLPI_USB_VID": ("usb", "vid"),
    "MLPI_USB_PID": ("usb", "pid"),
    "MLPI_USB_FUNC": ("usb", "function"),
    "MLPI_VARIANT": ("experiment", "fixed_variant"),
    "MLPI_EXPERIMENT_MODE": ("experiment", "mode"),
    "MLPI_SESSION_ROOT": ("session", "root"),
    "MLPI_LOG_FILE": ("logging", "file"),
    "MLPI_LOG_LEVEL": ("logging", "level"),
}


SYSTEM_CONFIG = Path("/etc/mlpi/mlpi.toml")
BOOT_CONFIG = Path("/boot/firmware/mlpi.toml")
PROJECT_CONFIG = Path("config/mlpi.toml")


def load(path: str | Path | None = None) -> Config:
    """Load config, applying defaults → files → env overrides.

    ``path`` is the highest-priority file source, evaluated after the system, boot and
    project files but before environment variables.
    """
    cfg = Config()

    sources: list[Path] = []
    for candidate in (SYSTEM_CONFIG, BOOT_CONFIG, PROJECT_CONFIG):
        if candidate.is_file():
            sources.append(candidate)
    if path is not None:
        sources.append(Path(path))

    load_warnings.clear()
    for src in sources:
        try:
            with src.open("rb") as fh:
                _merge(cfg, tomllib.load(fh))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            if path is not None and src == Path(path):
                raise
            # A typo in the user-edited boot-partition file must not take the Pi
            # down in the car: skip the file, keep going with the other sources.
            message = f"ignoring broken config file {src}: {exc}"
            load_warnings.append(message)
            print(f"mlpi: WARNING: {message}", file=sys.stderr)

    _apply_env(cfg, os.environ)
    return cfg


# Problems found by the last load() — shown in the session summary.
load_warnings: list[str] = []


def _merge(cfg: Config, data: dict[str, Any]) -> None:
    for section_name, section_data in data.items():
        if not isinstance(section_data, dict):
            continue
        section = getattr(cfg, section_name, None)
        if not is_dataclass(section):
            continue
        valid = {f.name for f in fields(section)}
        for key, value in section_data.items():
            if key in valid:
                setattr(section, key, value)


def _apply_env(cfg: Config, env: dict[str, str] | os._Environ) -> None:
    for var, (section_name, key) in ENV_OVERRIDES.items():
        raw = env.get(var)
        if raw is None or raw == "":
            continue
        section = getattr(cfg, section_name)
        target_type = type(getattr(section, key))
        try:
            value: Any = target_type(raw) if target_type is not str else raw
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{var}={raw!r} cannot be coerced to {target_type.__name__}") from exc
        setattr(section, key, value)


def as_dict(cfg: Config) -> dict[str, dict[str, Any]]:
    """Flatten the config into plain dicts (for the session info file)."""
    return {
        f.name: {g.name: getattr(section, g.name) for g in fields(section)}
        for f in fields(cfg)
        for section in (getattr(cfg, f.name),)
    }


def resolve_log_file(cfg: Config) -> Path:
    """Return the log file path, falling back to a writable user-state dir if unset."""
    if cfg.logging.file:
        return Path(cfg.logging.file)
    system = Path("/var/log/mlpi/mlpi.log")
    if _writable_dir(system.parent):
        return system
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return state_root / "mlpi" / "mlpi.log"


def _writable_dir(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.W_OK)
    except OSError:
        return False
