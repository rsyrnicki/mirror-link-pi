"""Configuration loading for MirrorLink-Pi.

Resolution order (later overrides earlier):
  1. Built-in defaults
  2. /etc/mlpi/mlpi.toml          (system)
  3. ./config/mlpi.toml           (project working dir)
  4. Path passed to ``load(path=...)``
  5. Environment variables (MLPI_*)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class NetworkConfig:
    interface: str = "usb0"
    address: str = ""           # empty = autodetect
    http_port: int = 8080
    # The VNC server we advertise to the head unit lives at this port on `address`.
    # It is run as a separate systemd unit (see deploy/) — we just hand the URL out.
    vnc_port: int = 5900


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
    model_number: str = "0.1"
    device_type: str = "urn:schemas-upnp-org:device:TmServerDevice:1"


@dataclass
class LoggingConfig:
    file: str = ""              # empty = autodetect
    level: str = "DEBUG"


@dataclass
class Config:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    ssdp: SsdpConfig = field(default_factory=SsdpConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


# Map env-var name → (section, field). Only declared keys are honored; typos are silently ignored.
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "MLPI_INTERFACE": ("network", "interface"),
    "MLPI_ADDRESS": ("network", "address"),
    "MLPI_HTTP_PORT": ("network", "http_port"),
    "MLPI_VNC_PORT": ("network", "vnc_port"),
    "MLPI_DEVICE_UUID": ("ssdp", "device_uuid"),
    "MLPI_LOG_FILE": ("logging", "file"),
    "MLPI_LOG_LEVEL": ("logging", "level"),
}


SYSTEM_CONFIG = Path("/etc/mlpi/mlpi.toml")
PROJECT_CONFIG = Path("config/mlpi.toml")


def load(path: str | Path | None = None) -> Config:
    """Load config, applying defaults → files → env overrides.

    ``path`` is the highest-priority file source, evaluated after the system and project
    files but before environment variables.
    """
    cfg = Config()

    sources: list[Path] = []
    if SYSTEM_CONFIG.is_file():
        sources.append(SYSTEM_CONFIG)
    if PROJECT_CONFIG.is_file():
        sources.append(PROJECT_CONFIG)
    if path is not None:
        sources.append(Path(path))

    for src in sources:
        with src.open("rb") as fh:
            _merge(cfg, tomllib.load(fh))

    _apply_env(cfg, os.environ)
    return cfg


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
