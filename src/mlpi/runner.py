"""Top-level orchestration of the MirrorLink server on the Pi.

Starts, each in its own guarded thread (a crash restarts that component only):
  DHCP server · SSDP responder · HTTP/SOAP server · VNC server · status screen ·
  status LED · link monitor (USB carrier + idle watchdog)
and records everything into the per-boot session directory.
"""

from __future__ import annotations

import logging
import os
import platform
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__, gadget, netinfo
from . import config as config_mod
from .canvas import Canvas
from .config import Config, as_dict, resolve_log_file
from .dhcp import DhcpServer
from .http_descriptor import DescriptorServer
from .led import StatusLed
from .rfb import RfbServer
from .screen import StatusScreen
from .session import STAGE_USB_LINK, Session
from .ssdp import SsdpResponder
from .variants import DEFAULT_VARIANTS_FILE, Variant, VariantManager, load_variants

log = logging.getLogger("mlpi")


def setup_logging(cfg: Config, session: Session | None = None) -> Path:
    if cfg.logging.file:
        log_path = Path(cfg.logging.file)
    elif session is not None:
        log_path = session.directory / "mlpi.log"
    else:
        log_path = resolve_log_file(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.logging.level.upper(), logging.DEBUG))
    root.addHandler(handler)
    root.addHandler(stream)
    return log_path


def resolve_address(cfg: Config, *, wait: float = 30.0) -> str:
    if cfg.network.address:
        return cfg.network.address
    deadline = time.monotonic() + wait
    while True:
        try:
            return netinfo.interface_address(cfg.network.interface)
        except (netinfo.InterfaceNotFoundError, RuntimeError) as exc:
            if time.monotonic() > deadline:
                raise
            log.info("waiting for an address on %s: %s", cfg.network.interface, exc)
            time.sleep(1.0)


def guarded(name: str, target: Callable[[], None], stop: threading.Event,
            session: Session | None) -> threading.Thread:
    """Run ``target`` in a daemon thread; restart it after a crash until ``stop``."""
    def loop() -> None:
        while not stop.is_set():
            try:
                target()
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("component %s crashed; restarting in 3 s", name)
                if session:
                    session.event("component_crash", component=name, error=repr(exc))
                stop.wait(3.0)
    thread = threading.Thread(target=loop, name=name, daemon=True)
    thread.start()
    return thread


class LinkMonitor:
    """Watches the USB carrier, and re-plugs the gadget when the car has gone quiet."""

    def __init__(self, cfg: Config, session: Session, ssdp: SsdpResponder,
                 rfb: RfbServer | None) -> None:
        self.cfg = cfg
        self.session = session
        self.ssdp = ssdp
        self.rfb = rfb
        self.reconnects = 0
        self._stop = threading.Event()

    def run(self) -> None:
        ifname = self.cfg.network.interface
        last: bool | None = None
        link_since = time.monotonic()
        idle = self.cfg.watchdog.idle_reconnect_seconds
        while not self._stop.wait(0.5):
            up = gadget.carrier(ifname)
            if up != last:
                self.session.event("usb_link", up=up, udc_state=gadget.udc_state())
                log.info("USB link %s", "UP" if up else "DOWN")
                if up:
                    self.session.reach(STAGE_USB_LINK)
                    self.ssdp.announce()
                    link_since = time.monotonic()
                last = up
            if not up or idle <= 0 or self.reconnects >= self.cfg.watchdog.max_reconnects:
                continue
            if self.rfb is not None and self.rfb.active > 0:
                continue
            quiet_since = max(link_since, self.session.last_car_activity)
            if time.monotonic() - quiet_since < idle:
                continue
            self.reconnects += 1
            log.warning("car silent for %ds: soft re-plug #%d", idle, self.reconnects)
            self.session.event("soft_reconnect", number=self.reconnects, idle_seconds=idle)
            gadget.soft_reconnect()
            link_since = time.monotonic()

    def stop(self) -> None:
        self._stop.set()


def _variant_manager(cfg: Config, variants_file: Path, session: Session) -> VariantManager:
    """Build the VariantManager; a broken variants file or setting falls back to baseline."""
    try:
        return VariantManager(
            load_variants(variants_file),
            mode=cfg.experiment.mode,
            fixed_variant=cfg.experiment.fixed_variant,
            attempt_gap_seconds=cfg.experiment.attempt_gap_seconds,
            session=session,
            state_dir=Path(cfg.session.root),
        )
    except Exception as exc:  # noqa: BLE001 - never lose a car trip to a typo
        message = f"variants unusable ({exc!r}); running baseline only"
        log.error(message)
        session.note("CONFIG WARNING", message)
        return VariantManager([Variant()], mode="fixed", fixed_variant="baseline",
                              session=session)


def run(cfg: Config) -> int:
    session = Session.open_for_boot(Path(cfg.session.root))
    log_path = setup_logging(cfg, session)
    log.info("mlpi %s starting, session %s, log %s", __version__, session.directory, log_path)

    for warning in config_mod.load_warnings:
        log.warning(warning)
        session.note("CONFIG WARNING", warning)

    variants_file = Path(cfg.experiment.variants_file) if cfg.experiment.variants_file \
        else DEFAULT_VARIANTS_FILE
    variants = _variant_manager(cfg, variants_file, session)
    session.write_info({
        "mlpi_version": __version__,
        "boot_number": session.boot_number,
        "python": sys.version,
        "platform": platform.platform(),
        "pid": os.getpid(),
        "config": as_dict(cfg),
        "variants": variants.names(),
        "first_variant": variants.current.name,
        "variants_file": str(variants_file),
    })
    session.note("current variant", f"{variants.current.name} (waiting for the car)")

    address = resolve_address(cfg)
    location = f"http://{address}:{cfg.network.http_port}/"
    log.info("address %s on %s", address, cfg.network.interface)

    stop = threading.Event()

    led = StatusLed(cfg.led.path) if cfg.led.enabled else None
    if led:
        session.on_stage(led.set_stage)
        guarded("led", led.run, stop, session)

    canvas = Canvas(cfg.vnc.width, cfg.vnc.height)
    screen = StatusScreen(
        canvas, session=session,
        variant_name=lambda: variants.current.name + (" (LOCKED)" if variants.locked else ""))
    guarded("screen", screen.run, stop, session)

    http_server = DescriptorServer(cfg, address, session=session, variants=variants)
    guarded("http", http_server.serve_forever, stop, session)

    ssdp = SsdpResponder(cfg, address=address, location=location, session=session)
    guarded("ssdp", ssdp.serve_forever, stop, session)

    dhcp = None
    if cfg.dhcp.enabled:
        prefix = int(cfg.usb.address.partition("/")[2] or 24)
        dhcp = DhcpServer(
            interface=cfg.network.interface, server_ip=address, prefix=prefix,
            client_ip=cfg.dhcp.client_address, lease_seconds=cfg.dhcp.lease_seconds,
            offer_router=cfg.dhcp.offer_router, offer_dns=cfg.dhcp.offer_dns,
            session=session)
        guarded("dhcp", dhcp.serve_forever, stop, session)

    rfb = None
    if cfg.vnc.enabled:
        rfb = RfbServer(
            bind_address=address, port=cfg.network.vnc_port, canvas=canvas,
            name=cfg.vnc.name, session=session,
            on_connect=lambda peer: variants.vnc_connected(),
            screen=screen, dump_dir=session.directory, dump_limit=cfg.vnc.raw_dump_limit)
        guarded("rfb", rfb.serve_forever, stop, session)

    link = LinkMonitor(cfg, session, ssdp, rfb)
    guarded("link", link.run, stop, session)

    def handle_signal(signum, _frame):
        log.info("Received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    session.event("started", address=address, pid=os.getpid())
    log.info("MirrorLink-Pi running. PID=%s", os.getpid())

    try:
        stop.wait()
    finally:
        for component in (link, dhcp, rfb, ssdp, screen, led):
            if component is not None:
                component.stop()
        http_server.shutdown()
        session.event("stopped")
        session.close()
    return 0
