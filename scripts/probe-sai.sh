#!/usr/bin/env bash
# Read-only probe of VW SAI on usb0.
#
# Captures the periodic UDP beacon on 192.168.7.255:28500 and the TCP server on
# 192.168.7.44:25010, then connects twice (once silent, once sending a single
# newline) to see what the head unit emits to a fresh client.
#
# Run on the Pi while the car is plugged in and the head unit has DHCP'd to
# 192.168.7.44. Does NOT touch the MirrorLink SOAP stack — completely separate
# protocol, separate listener.
#
# Usage:  sudo ./scripts/probe-sai.sh [output-dir]   (default /tmp/sai-probe)

set -euo pipefail

OUTDIR="${1:-/tmp/sai-probe}"
IFACE="usb0"
CAR_IP="192.168.7.44"
SAI_PORT="25010"
BEACON_PORT="28500"
CAPTURE_SECONDS="60"

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (tcpdump on $IFACE needs caps)." >&2
    exit 1
fi

if ! ip link show "$IFACE" &>/dev/null; then
    echo "Interface $IFACE does not exist — is the Pi gadget link up?" >&2
    exit 1
fi

for cmd in tcpdump nc; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Missing required tool: $cmd" >&2
        exit 1
    fi
done

install -d -m 0755 "$OUTDIR"

PCAP="$OUTDIR/sai.pcap"
BANNER="$OUTDIR/banner.txt"
NEWLINE="$OUTDIR/probe-newline.txt"

echo "Starting ${CAPTURE_SECONDS}s tcpdump on $IFACE → $PCAP" >&2
# -U flushes packets immediately so a clean SIGTERM from `timeout` keeps the
# pcap intact. The whole tcpdump call is wrapped so it exits without us having
# to track and signal it from the foreground.
timeout --signal=TERM "$CAPTURE_SECONDS" \
    tcpdump -i "$IFACE" -U -w "$PCAP" \
        "(udp port $BEACON_PORT) or (tcp port $SAI_PORT)" \
        >/dev/null 2>"$OUTDIR/tcpdump.stderr" &
TCPDUMP_PID=$!

# Give tcpdump time to attach + at least one beacon to land (beacons are 5s cadence).
sleep 6

echo "Connect 1 — silent (read banner only) → $BANNER" >&2
nc -w 10 "$CAR_IP" "$SAI_PORT" </dev/null >"$BANNER" 2>&1 || true

sleep 5

echo "Connect 2 — single newline → $NEWLINE" >&2
printf '\n' | nc -w 10 "$CAR_IP" "$SAI_PORT" >"$NEWLINE" 2>&1 || true

# Let the rest of the capture window run so any late-arriving beacons land too.
wait "$TCPDUMP_PID" 2>/dev/null || true

echo
echo "Done. Artifacts in $OUTDIR:"
ls -la "$OUTDIR"
echo
echo "Pull to laptop with:"
echo "  scp 'mlpi@mlpi.local:$OUTDIR/*' captures/2026-05-02_session3-sai/"
