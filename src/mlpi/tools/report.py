"""Summarise a session directory brought back from the car.

    python3 -m mlpi report path/to/sessions/0007

Reads events.jsonl and prints: furthest stage, per-attempt table (which variant,
what the car did), the car's DHCP fingerprint, anything we did not handle, VNC
activity and crashes. The raw material (pcap, journal, full HTTP bodies) stays in
the directory for deeper digging.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from ..session import STAGE_NAMES


def load_events(directory: Path) -> list[dict]:
    events = []
    path = directory / "events.jsonl"
    if not path.is_file():
        return events
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"kind": "corrupt_line", "t": None, "raw": line[:200]})
    return events


def _step_name(ev: dict) -> str:
    headers = {k.lower(): v for k, v in (ev.get("headers") or {}).items()}
    action = headers.get("soapaction", "")
    if action:
        return action.strip('"').rsplit("#", 1)[-1]
    return f"{ev.get('method')} {ev.get('path')}"


def summarise(directory: Path) -> str:
    events = load_events(directory)
    out: list[str] = [f"=== {directory} ==="]
    if not events:
        out.append("no events.jsonl (or empty) — did mlpi.service start? check journal.txt")
        return "\n".join(out)
    info = {}
    try:
        info = json.loads((directory / "info.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass
    if info:
        out.append(f"mlpi {info.get('mlpi_version')}, boot #{info.get('boot_number')}, "
                   f"variants {info.get('variants')}")

    stages = {e["stage"]: e["t"] for e in events if e.get("kind") == "stage"}
    best = max(stages) if stages else 0
    out.append(f"FURTHEST STAGE: {best} — {STAGE_NAMES.get(best, '?')}")
    for stage, t in sorted(stages.items()):
        out.append(f"  t={t:>8}  {stage} {STAGE_NAMES.get(stage, '?')}")

    # Attempts
    attempts: list[dict] = []
    current: dict | None = None
    for ev in events:
        kind = ev.get("kind")
        if kind == "attempt_start":
            current = {"n": ev["attempt"], "t": ev["t"], "variant": ev["variant"],
                       "simulated": ev.get("simulated"), "steps": []}
            attempts.append(current)
        elif kind == "http_request" and current is not None:
            step = _step_name(ev)
            if not current["steps"] or current["steps"][-1] != step:
                current["steps"].append(step)
        elif kind == "vnc_connect" and current is not None:
            current["steps"].append("** VNC CONNECT **")
    if attempts:
        out.append("")
        out.append(f"Attempts ({len(attempts)}):")
        for a in attempts:
            sim = " (simulator)" if a["simulated"] else ""
            out.append(f"  #{a['n']:<3} t={a['t']:>8}  {a['variant']:<16}{sim} "
                       + " > ".join(a["steps"]))
        per_variant: dict[str, Counter] = {}
        for a in attempts:
            c = per_variant.setdefault(a["variant"], Counter())
            c["attempts"] += 1
            c["launch"] += "LaunchApplication" in a["steps"]
            c["vnc"] += "** VNC CONNECT **" in a["steps"]
        out.append("")
        out.append("Per variant: attempts / reached LaunchApplication / VNC connect")
        for name, c in per_variant.items():
            out.append(f"  {name:<16} {c['attempts']:>3} / {c['launch']:>3} / {c['vnc']:>3}")

    locked = [e for e in events if e.get("kind") == "variant_locked"]
    if locked:
        out.append(f"\n*** WINNING VARIANT: {locked[0]['variant']} ***")

    # DHCP fingerprint
    dhcp = [e for e in events if e.get("kind") == "dhcp_rx"]
    if dhcp:
        opts = dhcp[0].get("options", {})
        out.append("")
        out.append(f"Car DHCP: mac {dhcp[0].get('mac')}, {len(dhcp)} packets, "
                   f"first options {opts}")

    # Links, reconnects, crashes
    links = [e for e in events if e.get("kind") == "usb_link"]
    out.append("")
    out.append(f"USB link changes: {len(links)}; soft re-plugs: "
               f"{sum(1 for e in events if e.get('kind') == 'soft_reconnect')}")
    for e in events:
        if e.get("kind") == "component_crash":
            out.append(f"  CRASH t={e['t']} {e.get('component')}: {e.get('error')}")

    # Unhandled HTTP
    odd = [e for e in events if e.get("kind") == "http_response" and e.get("status", 200) >= 400]
    if odd:
        out.append("")
        out.append("HTTP errors we returned:")
        for e in odd[:30]:
            out.append(f"  t={e['t']} {e.get('method')} {e.get('path')} → {e.get('status')}")

    actions = Counter(_step_name(e) for e in events if e.get("kind") == "http_request")
    out.append("")
    out.append("Requests from the car: " + ", ".join(f"{k}×{v}" for k, v in actions.items()))

    # VNC
    vnc = [e for e in events if str(e.get("kind", "")).startswith("vnc_")]
    if vnc:
        out.append("")
        out.append("VNC events:")
        for e in vnc[:60]:
            detail = {k: v for k, v in e.items() if k not in ("t", "wall", "kind")}
            out.append(f"  t={e['t']} {e['kind']} {detail}")
        dumps = sorted(directory.glob("vnc-*-rx.bin"))
        for d in dumps:
            data = d.read_bytes()
            out.append(f"  {d.name}: {len(data)} bytes, first 64: {data[:64].hex()}")

    # USB level (written by the gadget daemon into usb.jsonl)
    usb_path = directory / "usb.jsonl"
    if usb_path.is_file():
        usb = []
        for line in usb_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                usb.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        out.append("")
        up = next((e for e in usb if e.get("kind") == "gadget_up"), {})
        out.append(f"USB gadget: ml_command={up.get('ml_command')}, "
                   f"VID:PID {up.get('vid')}:{up.get('pid')}")
        cmds = [e for e in usb if e.get("kind") == "ml_usb_command"]
        if cmds:
            first = cmds[0]
            out.append(f"  MirrorLink USB command received {len(cmds)}×: car speaks "
                       f"MirrorLink {first.get('ml_version')}, host VID {first.get('host_vid')}")
        else:
            out.append("  no MirrorLink USB command received")
        states = [e.get("state") for e in usb if e.get("kind") == "udc_state"]
        out.append(f"  UDC states: {' > '.join(states[:20])}")
        for e in usb:
            if e.get("kind") in ("ffs_fallback", "ffs_setup_stalled", "ffs_read_error"):
                detail = {k: v for k, v in e.items() if k not in ("wall", "kind")}
                out.append(f"  {e['kind']}: {detail}")

    dap = [e for e in events if str(e.get("kind", "")).startswith("dap_")]
    if dap:
        out.append("")
        out.append("DAP (device attestation):")
        for e in dap[:20]:
            detail = {k: v for k, v in e.items() if k not in ("t", "wall", "kind", "xml")}
            out.append(f"  t={e['t']} {e['kind']} {detail}")

    profile = [e for e in events if e.get("kind") == "client_profile"]
    if profile:
        model = re.search(r"<modelName>([^<]*)", profile[-1].get("xml", ""))
        out.append("")
        out.append(f"Car client profile: {model.group(1) if model else '?'} "
                   f"({len(profile[-1].get('xml', ''))} bytes)")
    return "\n".join(out)


def run(directories: list[Path]) -> int:
    for d in directories:
        print(summarise(d))
        print()
    return 0
