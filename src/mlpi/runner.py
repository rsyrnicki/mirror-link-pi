"""Top-level orchestration: start SSDP responder + HTTP descriptor server, wait for signals."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from . import netinfo
from .config import Config, resolve_log_file
from .http_descriptor import DescriptorServer
from .ssdp import SsdpResponder

log = logging.getLogger("mlpi")


def setup_logging(cfg: Config) -> Path:
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


def resolve_address(cfg: Config) -> str:
    if cfg.network.address:
        return cfg.network.address
    return netinfo.interface_address(cfg.network.interface)


def run(cfg: Config) -> int:
    log_path = setup_logging(cfg)
    log.info("Logging to %s", log_path)

    address = resolve_address(cfg)
    location = f"http://{address}:{cfg.network.http_port}/"
    log.info("Resolved address %s for interface %s", address, cfg.network.interface)

    http_server = DescriptorServer(cfg, address)
    ssdp = SsdpResponder(cfg, address=address, location=location)

    http_thread = threading.Thread(target=http_server.serve_forever, name="http", daemon=True)
    ssdp_thread = threading.Thread(target=ssdp.serve_forever, name="ssdp", daemon=True)

    stop = threading.Event()

    def handle_signal(signum, _frame):
        log.info("Received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    http_thread.start()
    ssdp_thread.start()
    log.info("MirrorLink-Pi running. PID=%s", os.getpid())

    try:
        stop.wait()
    finally:
        ssdp.stop()
        http_server.shutdown()
        ssdp_thread.join(timeout=5)
        http_thread.join(timeout=5)

    return 0
