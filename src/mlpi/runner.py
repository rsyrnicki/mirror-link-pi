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
from .dap import DapServer
from .dhcp import DhcpServer
from .http_descriptor import DescriptorServer
from .led import StatusLed
from .rfb import RfbServer
from .screen import StatusScreen
from .session import STAGE_USB_LINK, Session
from .ssdp import SsdpResponder
from .variants import DEFAULT_VARIANTS_FILE, Variant, VariantManager, load_variants
from .video import DisplaySwitch, InputRouter

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
                 rfb: RfbServer | None,
                 on_interface_recreated: Callable[[], None] = lambda: None) -> None:
        self.cfg = cfg
        self.session = session
        self.ssdp = ssdp
        self.rfb = rfb
        self.reconnects = 0
        self.on_interface_recreated = on_interface_recreated
        self._stop = threading.Event()

    def run(self) -> None:
        ifname = self.cfg.network.interface
        last: bool | None = None
        link_since = time.monotonic()
        idle = self.cfg.watchdog.idle_reconnect_seconds
        ifindex = gadget._ifindex(ifname)
        while not self._stop.wait(0.5):
            # If the gadget was rebuilt, usb0 is a new device: sockets bound to the old
            # one (DHCP uses SO_BINDTODEVICE) are deaf. Restart the whole service.
            current = gadget._ifindex(ifname)
            if ifindex > 0 and current > 0 and current != ifindex:
                self.session.event("usb_interface_recreated", old=ifindex, new=current)
                log.warning("%s was recreated (ifindex %d → %d): restarting", ifname,
                            ifindex, current)
                self.on_interface_recreated()
                return
            if ifindex <= 0:
                ifindex = current
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


def context_info(variant: Variant) -> tuple[int, int, int, int]:
    """(appID, trust level, application category, content category) for VNC context
    information (Part 2 §8.3). The foreground app is the home screen when listed,
    else the stand-alone VNC server itself."""
    from .soap import HOME_APP_ID_INT, VNC_APP_ID_INT
    app_id = HOME_APP_ID_INT if variant.home_app else VNC_APP_ID_INT
    return (app_id, int(variant.context_trust_level, 16),
            int(variant.context_app_category, 16), 0)


def _variant_manager(cfg: Config, variants_file: Path, session: Session) -> VariantManager:
    """Build the VariantManager; a broken variants file falls back to the spec default."""
    try:
        return VariantManager(
            load_variants(variants_file),
            mode=cfg.experiment.mode,
            fixed_variant=cfg.experiment.fixed_variant,
            attempt_gap_seconds=cfg.experiment.attempt_gap_seconds,
            cycles_per_variant=cfg.experiment.cycles_per_variant,
            session=session,
            state_dir=Path(cfg.session.root),
            start_variant=cfg.experiment.start_variant,
        )
    except Exception as exc:  # noqa: BLE001 - never lose a car trip to a typo
        message = f"variants unusable ({exc!r}); running the spec-1.0 default only"
        log.error(message)
        session.note("CONFIG WARNING", message)
        return VariantManager([Variant()], mode="fixed", fixed_variant=Variant().name,
                              session=session)


def _start_phone_mode(cfg: Config, phone_link, router: InputRouter, stop: threading.Event,
                      session: Session) -> DhcpServer:
    """Hotspot + DHCP on the Wi-Fi interface + the scrcpy link (docs/phone-mode.md)."""
    from .phone import HotspotSettings, ensure_hotspot
    pc = cfg.phone
    server_ip, _, prefix = pc.address.partition("/")
    if pc.manage_hotspot:
        def hotspot() -> None:
            settings = HotspotSettings(pc.interface, pc.address, pc.wifi_ssid,
                                       pc.wifi_password, pc.wifi_country, pc.wifi_channel)
            # NetworkManager may still be starting: retry for ~2 minutes.
            for _attempt in range(12):
                result = ensure_hotspot(settings)
                log.info("phone: %s", result)
                session.event("phone_hotspot", result=result)
                if result.startswith("hotspot ") or result.startswith("no wifi_password") \
                        or stop.wait(10):
                    break
            session.note("phone hotspot", result)
        threading.Thread(target=hotspot, name="hotspot", daemon=True).start()

    wifi_dhcp = DhcpServer(interface=pc.interface, server_ip=server_ip, prefix=int(prefix or 24),
                           client_ip=pc.client_address, offer_router=False, offer_dns=False,
                           session=session, is_car=False)

    def serve_wifi_dhcp() -> None:
        iface = Path("/sys/class/net") / pc.interface
        while not iface.exists() and not stop.is_set():   # Wi-Fi may come up late
            stop.wait(2.0)
        if not stop.is_set():
            wifi_dhcp.serve_forever()
    guarded("wifi-dhcp", serve_wifi_dhcp, stop, session)

    def candidates() -> list[str]:
        from .health import wifi_stations
        # No station list (none associated, or `iw` missing; the two look alike):
        # every lease, as before.
        try:
            macs = [st["mac"] for st in wifi_stations(pc.interface)] or None
        except Exception:
            macs = None
        return wifi_dhcp.connected_addresses(macs)
    phone_link.candidates = candidates
    router.attach_phone(phone_link.frame, phone_link)
    guarded("phone", phone_link.run, stop, session)
    return wifi_dhcp


def run(cfg: Config) -> int:
    session = Session.open_for_boot(Path(cfg.session.root))
    log_path = setup_logging(cfg, session)
    log.info("mlpi %s starting, session %s, log %s", __version__, session.directory, log_path)

    for i, warning in enumerate(config_mod.load_warnings, 1):
        log.warning(warning)
        session.note(f"CONFIG WARNING {i}", warning)

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
    switch = DisplaySwitch(canvas)       # what the car sees: status screen or phone
    phone_link = None
    if cfg.phone.enabled:
        from .phone import PhoneLink
        phone_link = PhoneLink(cfg.phone, switch.new_video_frame(), switch, session=session,
                               local_ip=cfg.phone.address.partition("/")[0])
    screen = StatusScreen(
        canvas, session=session,
        variant_name=lambda: variants.current.name + (" (LOCKED)" if variants.locked else ""),
        phone_status=(lambda: phone_link.status) if phone_link else None)
    guarded("screen", screen.run, stop, session)
    router = InputRouter(screen, switch)

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
            bind_address=address, port=cfg.network.vnc_port, canvas=switch,
            name=cfg.vnc.name, session=session,
            on_connect=lambda peer: variants.vnc_connected(),
            screen=router, dump_dir=session.directory, dump_limit=cfg.vnc.raw_dump_limit,
            ml_version=lambda: variants.current.ml_version or "1.0",
            context_info=lambda: context_info(variants.current))
        guarded("rfb", rfb.serve_forever, stop, session)

    dap = DapServer(bind_address=address, port=cfg.network.dap_port, session=session)
    guarded("dap", dap.serve_forever, stop, session)

    from .health import HealthMonitor
    health = HealthMonitor(session, wifi_interface=cfg.phone.interface
                           if cfg.phone.enabled else "")
    guarded("health", health.run, stop, session)

    wifi_dhcp = None
    if phone_link is not None:
        wifi_dhcp = _start_phone_mode(cfg, phone_link, router, stop, session)

    exit_code = 0

    def restart_service() -> None:
        nonlocal exit_code
        exit_code = 75   # non-zero: systemd (Restart=always) starts us again
        stop.set()

    link = LinkMonitor(cfg, session, ssdp, rfb, on_interface_recreated=restart_service)
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
        for component in (link, phone_link, wifi_dhcp, health, dhcp, rfb, dap, ssdp, screen,
                          led):
            if component is not None:
                component.stop()
        http_server.shutdown()
        session.event("stopped", exit_code=exit_code)
        session.close()
    return exit_code
