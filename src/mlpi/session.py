"""Per-boot session recording.

Every boot of the Pi gets its own directory ``<root>/sessions/NNNN`` (NNNN = boot
counter; the Pi has no RTC and no network in the car, so wall-clock time is not
trustworthy). All components write into it:

  events.jsonl   structured timeline — one JSON object per line (this module)
  mlpi.log       human-readable Python log of the main service
  usb0.pcap      every frame on usb0 (mlpi.capture)
  journal.txt    the whole systemd journal of this boot, incl. kernel USB messages
  vnc-N-rx.bin   raw bytes the VNC client sent on connection N
  summary.txt    rewritten on every stage change: how far did we get?
  info.json      software version, config, boot number

The *stage* is the furthest point the car reached in the handshake during this boot.
It is sticky (never goes down) and drives the status LED and the on-screen app.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RUN_POINTER = Path("/run/mlpi/session-dir")

# Sticky stages, in handshake order.
STAGE_BOOT = 0
STAGE_USB_LINK = 1       # USB cable carrier up: the car enumerated our gadget
STAGE_DHCP = 2           # we handed the car an address
STAGE_UPNP = 3           # the car fetched our device descriptor
STAGE_LAUNCH = 4         # the car called LaunchApplication
STAGE_VNC_CONNECT = 5    # the car opened a TCP connection to our VNC port
STAGE_VNC_FRAMES = 6     # RFB handshake completed and we sent a framebuffer update

STAGE_NAMES = {
    STAGE_BOOT: "boot (no USB host yet)",
    STAGE_USB_LINK: "USB link up",
    STAGE_DHCP: "DHCP lease handed out",
    STAGE_UPNP: "UPnP descriptor fetched",
    STAGE_LAUNCH: "LaunchApplication received",
    STAGE_VNC_CONNECT: "VNC TCP connection opened",
    STAGE_VNC_FRAMES: "VNC frames sent",
}


# Event kinds that mean "the car just did something" (feeds the idle watchdog).
_CAR_ACTIVITY = frozenset({"dhcp_rx", "http_request", "ssdp_msearch"})


def _now_wall() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def init_session(root: Path, *, pointer: Path = RUN_POINTER) -> Path:
    """Create the directory for this boot and publish it via ``pointer``.

    Called once per boot by ``mlpi session-init`` (mlpi-session.service), before any
    other mlpi unit starts, so that all of them write into the same directory.
    """
    root.mkdir(parents=True, exist_ok=True)
    counter = root / "boot-count"
    try:
        n = int(counter.read_text().strip()) + 1
    except (FileNotFoundError, ValueError):
        n = 1
    _write_durable(counter, f"{n}\n")

    sessions = root / "sessions"
    sessions.mkdir(exist_ok=True)
    try:
        prune_sessions(sessions)
    except OSError as exc:
        log.warning("could not prune old sessions: %s", exc)
    directory = sessions / f"{n:04d}"
    directory.mkdir(exist_ok=True)

    current = sessions / "current"
    try:
        if current.is_symlink() or current.exists():
            current.unlink()
        current.symlink_to(directory.name)
    except OSError as exc:
        log.warning("could not update %s: %s", current, exc)

    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(directory) + "\n")
    return directory


def prune_sessions(sessions: Path, *, keep: int = 40, keep_pcaps: int = 5,
                   min_free: int = 1024 * 1024 * 1024) -> list[str]:
    """Delete old recordings so the SD card never fills up.

    Keeps the newest ``keep`` session directories, drops the pcaps (the big part) of
    all but the newest ``keep_pcaps``, then removes the oldest sessions while less than
    ``min_free`` bytes are free. Returns what was deleted.
    """
    dirs = sorted(p for p in sessions.iterdir() if p.is_dir() and not p.is_symlink()
                  and p.name.isdigit())
    removed = []
    for old in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    dirs = [d for d in dirs if d.name not in removed]
    for old in dirs[:-keep_pcaps] if len(dirs) > keep_pcaps else []:
        for pcap in old.glob("*.pcap"):
            pcap.unlink(missing_ok=True)
            removed.append(f"{old.name}/{pcap.name}")
    while len(dirs) > 1 and shutil.disk_usage(sessions).free < min_free:
        oldest = dirs.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        removed.append(f"{oldest.name} (low disk)")
    if removed:
        log.info("pruned old recordings: %s", ", ".join(removed))
    return removed


def current_session_dir(*, pointer: Path = RUN_POINTER) -> Path | None:
    try:
        text = pointer.read_text().strip()
    except FileNotFoundError:
        return None
    return Path(text) if text else None


def _write_durable(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class Session:
    """Thread-safe event log + sticky stage tracker for one boot."""

    def __init__(self, directory: Path, *, boot_number: int | None = None) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.boot_number = boot_number if boot_number is not None else _guess_boot_number(directory)
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._events = open(directory / "events.jsonl", "a", encoding="utf-8")
        self._dirty = False
        self.last_car_activity = time.monotonic()
        self.stage = STAGE_BOOT
        self.stage_times: dict[int, float] = {STAGE_BOOT: 0.0}
        self.counters: dict[str, int] = {}
        self.notes: dict[str, str] = {}
        self._stage_listeners: list[Callable[[int], None]] = []
        self._stop = threading.Event()
        self._syncer = threading.Thread(target=self._sync_loop, name="session-sync", daemon=True)
        self._syncer.start()

    # ----- construction helpers -----

    @classmethod
    def open_for_boot(cls, root: Path) -> Session:
        """Use the directory published by mlpi-session.service, or create one (dev runs)."""
        directory = current_session_dir()
        if directory is None or not directory.is_dir():
            directory = init_session(root, pointer=Path(root) / "session-dir.dev")
        return cls(directory)

    # ----- event log -----

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def event(self, kind: str, **fields: Any) -> None:
        if kind in _CAR_ACTIVITY or kind.startswith("vnc_"):
            self.last_car_activity = time.monotonic()
        record = {"t": round(self.elapsed(), 3), "wall": _now_wall(), "kind": kind}
        record.update(fields)
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            try:
                self._events.write(line + "\n")
                self._events.flush()
                self._dirty = True
            except (OSError, ValueError) as exc:
                log.error("event log write failed: %s", exc)

    def count(self, kind: str) -> int:
        with self._lock:
            return self.counters.get(kind, 0)

    def note(self, key: str, value: str) -> None:
        """Free-form facts shown in summary.txt (e.g. current variant, car's name)."""
        with self._lock:
            self.notes[key] = value
        self.write_summary()

    # ----- stages -----

    def on_stage(self, listener: Callable[[int], None]) -> None:
        self._stage_listeners.append(listener)
        listener(self.stage)

    def reach(self, stage: int, **fields: Any) -> None:
        """Record that the car reached ``stage``; only the first time per stage counts."""
        with self._lock:
            new = stage not in self.stage_times
            if new:
                self.stage_times[stage] = round(self.elapsed(), 3)
            raised = stage > self.stage
            if raised:
                self.stage = stage
        if new:
            self.event("stage", stage=stage, name=STAGE_NAMES.get(stage, str(stage)), **fields)
            log.info("STAGE %d reached: %s", stage, STAGE_NAMES.get(stage, stage))
            self.write_summary()
        if raised:
            for listener in list(self._stage_listeners):
                try:
                    listener(stage)
                except Exception:  # noqa: BLE001 - a broken LED must not break the log
                    log.exception("stage listener failed")

    # ----- summary -----

    def write_summary(self) -> None:
        with self._lock:
            lines = [
                f"MirrorLink-Pi session, boot #{self.boot_number}",
                f"written at t={self.elapsed():.1f}s "
                f"(wall clock {_now_wall()}, may be wrong: no RTC)",
                "",
                f"FURTHEST STAGE: {self.stage} — {STAGE_NAMES.get(self.stage, '?')}",
                "",
                "Stages reached (seconds since mlpi start):",
            ]
            for stage, t in sorted(self.stage_times.items()):
                lines.append(f"  {stage}  {STAGE_NAMES.get(stage, '?'):<30} t={t}")
            if self.notes:
                lines.append("")
                lines.append("Notes:")
                for key, value in sorted(self.notes.items()):
                    lines.append(f"  {key}: {value}")
            lines.append("")
            lines.append("Event counts:")
            for key, value in sorted(self.counters.items()):
                lines.append(f"  {key}: {value}")
            text = "\n".join(lines) + "\n"
        try:
            _write_durable(self.directory / "summary.txt", text)
        except OSError as exc:
            log.error("summary write failed: %s", exc)

    def write_info(self, info: dict[str, Any]) -> None:
        try:
            _write_durable(
                self.directory / "info.json",
                json.dumps(info, indent=2, default=str, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            log.error("info write failed: %s", exc)

    # ----- durability -----

    def _sync_loop(self) -> None:
        # The car cuts power when the ignition goes off. fsync every 2 s bounds the loss.
        while not self._stop.wait(2.0):
            self.sync()

    def sync(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            try:
                os.fsync(self._events.fileno())
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        self._stop.set()
        self.write_summary()
        self.sync()
        with self._lock:
            try:
                self._events.close()
            except OSError:
                pass


def _guess_boot_number(directory: Path) -> int:
    try:
        return int(directory.name)
    except ValueError:
        return 0
