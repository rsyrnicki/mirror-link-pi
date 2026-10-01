"""Protocol variants tried against the head unit, one per connection attempt.

We cannot debug in the car, and each car trip is expensive. The VW MIB II retries
the whole MirrorLink handshake by itself roughly every 10 s (DHCP → descriptor →
profile → app list → launch → status → silence → again). So instead of betting a
whole trip on one guess, the server switches to the next *variant* at the start of
each new attempt, and records which variant was active. The first variant that
makes the car open a TCP connection to the VNC port is locked in for the rest of
the boot and remembered as the "winner" for the next boot.

A new attempt starts when the car fetches the root device descriptor and either
  * the previous attempt already got its application list (one full handshake cycle), or
  * at least ``attempt_gap_seconds`` of silence passed.
The first rule matters: the MIB2 (session 2026-09-28) loops descriptor → profile →
app list every ~2.7 s without any pause, so a pure silence rule never saw a second
attempt. Only the descriptor fetch starts an attempt, so a slow status poll inside
one attempt can never switch variants mid-handshake. Each variant is kept for
``cycles_per_variant`` attempts before moving on.

Variants are defined in ``config/variants.toml`` so they can be edited on the SD card
without touching code.
"""

from __future__ import annotations

import logging
import threading
import time
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .session import Session

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_VARIANTS_FILE = _REPO_ROOT / "config" / "variants.toml"

SIMULATOR_USER_AGENT = "mlpi-simulate-car"

# A VNC connect up to this long after a LaunchApplication counts for that launch.
LAUNCH_TO_VNC_SECONDS = 30.0


@dataclass(frozen=True)
class Variant:
    """Knobs that change what the head unit sees.

    Defaults follow ETSI TS 103 544 v1.3.1 (see docs/spec-notes.md) for a
    MirrorLink 1.0 server offering one stand-alone VNC server.
    """

    name: str = "spec-1.0"
    description: str = ""
    # Device XML: "" = omit <X_mirrorLinkVersion> (Part 13 §7.6: client then treats
    # us as 1.0, which needs no DAP). "1.1" etc. = declare that version.
    ml_version: str = ""
    # AppListing entry of the stand-alone VNC server. Part 9 §5.2.1: protocolID VNC,
    # appCategory 0xF0000001 ("Server functionality").
    app_category: str = "0xF0000001"
    app_trust_level: str = ""            # "" = omit <appInfo><trustLevel>
    audio_info: bool = False             # <audioInfo> block (Part 9: for audio links)
    audio_trust_level: str = ""          # "" = omit <audioInfo><trustLevel>
    display_content_category: str = ""   # "" = omit <displayInfo>
    cert_url: bool = False               # <appCertificateURL>
    # Namespace on <appList> (the XSD defines one; the spec's example omits it).
    applist_namespace: bool = True
    # Extra entries: a VNC "home screen" UI application (Part 9 Annex A 0x00010001)
    # and a Device Attestation Protocol endpoint (Part 9 §5.2.5, Part 4).
    home_app: bool = False
    dap: bool = False
    # RTP audio server + client entries for payloads 98/99, as a Galaxy S6 lists them
    # (the MIB2's client profile announces exactly those payloads).
    rtp_apps: bool = False
    # Bluetooth A2DP + HFP audio entries and <X_connectivity><bluetooth> with the
    # phone's address (Part 3 §6.3, Part 9 §5.2.3, Part 12): lets the car pick the
    # phone's Bluetooth as the audio link. Needs the phone's address (see btaddr.py).
    bt_apps: bool = False
    # <allowedProfileIDs>0</allowedProfileIDs> in every entry (the S6 does this).
    allowed_profile_ids: bool = False
    # LaunchApplication AppURI: "<scheme>://<address>:<port>" (Part 9 Table 4-7).
    uri_scheme: str = "VNC"
    # VNC Context Information (Part 2 §8.3) sent with framebuffer updates.
    context_app_category: str = "0x00010001"   # Home screen
    context_trust_level: str = "0x0080"


def load_variants(path: Path | None = None) -> list[Variant]:
    path = path or DEFAULT_VARIANTS_FILE
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    valid = {f.name for f in fields(Variant)}
    out: list[Variant] = []
    for entry in data.get("variant", []):
        unknown = set(entry) - valid
        if unknown:
            log.warning("variant %r: ignoring unknown keys %s", entry.get("name"), sorted(unknown))
        out.append(Variant(**{k: v for k, v in entry.items() if k in valid}))
    if not out:
        out.append(Variant())
    names = [v.name for v in out]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate variant names in {path}: {names}")
    return out


class VariantManager:
    """Chooses the active variant and tracks attempts and their outcome."""

    def __init__(
        self,
        variants: list[Variant],
        *,
        mode: str = "rotate",
        fixed_variant: str = "spec-1.0",
        attempt_gap_seconds: float = 4.0,
        cycles_per_variant: int = 2,
        session: Session | None = None,
        state_dir: Path | None = None,
        clock=time.monotonic,
        start_variant: str = "",
    ) -> None:
        if not variants:
            raise ValueError("need at least one variant")
        self._variants = list(variants)
        self._mode = mode
        self._gap = attempt_gap_seconds
        self._cycles = max(1, int(cycles_per_variant))
        self._attempts_on_variant = 0
        # (variant index, time, simulated) of the last LaunchApplication, so a VNC
        # connect that arrives after the car already started its next cycle still
        # credits the variant that handed out the AppURI.
        self._last_launch: tuple[int, float, bool] | None = None
        self._session = session
        self._clock = clock
        self._lock = threading.Lock()
        self._winner_file = state_dir / "winner-variant" if state_dir else None
        self._index = 0
        self._locked = False
        self._last_request = float("-inf")
        self.attempt = 0
        self._attempt_simulated = False
        self._attempt_progress: set[str] = set()

        if mode == "fixed":
            self._index = self._find(fixed_variant)
            self._locked = True
        elif mode == "rotate":
            previous = start_variant or self._read_winner()
            if previous is not None:
                # Start with last boot's winner so a working setup comes up first.
                idx = self._find(previous, default=None)
                if idx is not None:
                    self._index = idx
        else:
            raise ValueError(f"experiment mode must be 'rotate' or 'fixed', got {mode!r}")

    # ----- queries -----

    @property
    def current(self) -> Variant:
        with self._lock:
            return self._variants[self._index]

    @property
    def locked(self) -> bool:
        return self._locked

    def names(self) -> list[str]:
        return [v.name for v in self._variants]

    # ----- request hooks -----

    def on_request(self, *, is_root_descriptor: bool, user_agent: str = "") -> Variant:
        """Call for every HTTP request from the car. Returns the variant to use."""
        now = self._clock()
        with self._lock:
            cycle_done = "applist" in self._attempt_progress
            silence = (now - self._last_request) >= self._gap
            starts_attempt = is_root_descriptor and (self.attempt == 0 or cycle_done or silence)
            self._last_request = now
            if starts_attempt:
                if self.attempt > 0 and not self._locked:
                    self._attempts_on_variant += 1
                    if self._attempts_on_variant >= self._cycles:
                        self._index = (self._index + 1) % len(self._variants)
                        self._attempts_on_variant = 0
                self.attempt += 1
                self._attempt_simulated = SIMULATOR_USER_AGENT in user_agent
                self._attempt_progress = set()
                variant = self._variants[self._index]
                attempt = self.attempt
            else:
                variant = self._variants[self._index]
                attempt = None
        if attempt is not None:
            log.info("attempt #%d starts with variant %r%s", attempt, variant.name,
                     " (simulated)" if self._attempt_simulated else "")
            if self._session:
                self._session.event("attempt_start", attempt=attempt, variant=variant.name,
                                    simulated=self._attempt_simulated, user_agent=user_agent)
                self._session.note("current variant", f"{variant.name} (attempt #{attempt})")
        return variant

    def progress(self, step: str) -> None:
        """Record that the current attempt reached ``step`` (e.g. 'launch', 'vnc')."""
        with self._lock:
            if step in self._attempt_progress:
                return
            self._attempt_progress.add(step)
            attempt, variant = self.attempt, self._variants[self._index]
            if step == "launch":
                self._last_launch = (self._index, self._clock(), self._attempt_simulated)
        if self._session:
            self._session.event("attempt_progress", attempt=attempt, variant=variant.name,
                                step=step)

    def vnc_connected(self) -> None:
        """The car opened the VNC port: lock the variant that got us here.

        Only counts when it follows a LaunchApplication in the same attempt — a VNC
        viewer on the laptop during the home pre-flight check must not lock anything.
        """
        with self._lock:
            simulated = self._attempt_simulated
            after_launch = "launch" in self._attempt_progress
            recent = self._last_launch
            if not after_launch and recent and self._clock() - recent[1] <= LAUNCH_TO_VNC_SECONDS:
                # The car already began a new cycle (maybe on another variant) before
                # connecting: credit the variant that answered the launch.
                if not self._locked:
                    self._index = recent[0]
                simulated, after_launch = recent[2], True
            variant = self._variants[self._index]
            newly_locked = not self._locked and not simulated and after_launch
            if newly_locked:
                self._locked = True
        self.progress("vnc")
        if simulated or not after_launch:
            if self._session and not after_launch:
                self._session.event("vnc_without_launch", variant=variant.name)
            return
        if newly_locked:
            log.info("variant %r made the car connect to VNC: locking it", variant.name)
            if self._session:
                self._session.event("variant_locked", variant=variant.name, attempt=self.attempt)
                self._session.note("WINNING VARIANT", variant.name)
            self._write_winner(variant.name)

    # ----- helpers -----

    def _find(self, name: str, default: int | None | str = "raise") -> int | None:
        for i, v in enumerate(self._variants):
            if v.name == name:
                return i
        if default == "raise":
            raise ValueError(f"unknown variant {name!r}; known: {self.names()}")
        return default  # type: ignore[return-value]

    def _read_winner(self) -> str | None:
        if not self._winner_file:
            return None
        try:
            return self._winner_file.read_text().strip() or None
        except OSError:
            return None

    def _write_winner(self, name: str) -> None:
        if not self._winner_file:
            return
        try:
            self._winner_file.write_text(name + "\n")
        except OSError as exc:
            log.warning("could not persist winner variant: %s", exc)
