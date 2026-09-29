#!/usr/bin/env bash
# Active SAI probes — send a candidate XML envelope per fresh TCP connection
# and capture the server's response. Follows probe-sai.sh which only did silent
# connects; v2 actually requests something.
#
# Per probe: connect → drain hello (1s) → send envelope → read 5s → close.
# Per-probe output is annotated with HELLO / PROBE SENT / RESPONSE markers.
# A single tcpdump covers the whole window.
#
# Run on the Pi while the car is plugged in. SAI uses ports separate from
# MirrorLink (UDP 28500, TCP 25010 vs. 1900/8080/5900) — won't interfere.
#
# Usage:  sudo ./scripts/probe-sai-v2.sh [output-dir]   (default /tmp/sai-probe-v2)

set -euo pipefail

OUTDIR="${1:-/tmp/sai-probe-v2}"
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

for cmd in tcpdump python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Missing required tool: $cmd" >&2
        exit 1
    fi
done

install -d -m 0755 "$OUTDIR"

PCAP="$OUTDIR/sai-v2.pcap"
echo "Starting ${CAPTURE_SECONDS}s tcpdump on $IFACE → $PCAP" >&2
timeout --signal=TERM "$CAPTURE_SECONDS" \
    tcpdump -i "$IFACE" -U -w "$PCAP" \
        "(udp port $BEACON_PORT) or (tcp port $SAI_PORT)" \
        >/dev/null 2>"$OUTDIR/tcpdump.stderr" &
TCPDUMP_PID=$!

# Let tcpdump attach + at least one beacon arrive.
sleep 2

PROBE_HELPER="$OUTDIR/probe-helper.py"
cat > "$PROBE_HELPER" <<'PYEOF'
import socket, sys, time

host, port, probe, outfile = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]

s = socket.socket()
s.settimeout(5)
s.connect((host, port))

# Drain the server's initial hello (up to 1 s).
hello = b""
s.settimeout(1.0)
try:
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        hello += chunk
except socket.timeout:
    pass

# Convert literal "\n" tokens in the probe argument into real newlines so we
# can pass envelopes from a shell script via argv without escaping nightmares.
probe_bytes = probe.encode().replace(b"\\n", b"\n")
s.sendall(probe_bytes)
time.sleep(0.1)

response = b""
s.settimeout(5.0)
try:
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        response += chunk
except socket.timeout:
    pass

try:
    s.shutdown(socket.SHUT_WR)
except OSError:
    pass
s.close()

with open(outfile, "wb") as f:
    f.write(f"### HELLO ({len(hello)} bytes) ###\n".encode())
    f.write(hello)
    if not hello.endswith(b"\n"):
        f.write(b"\n")
    f.write(f"### PROBE SENT ({len(probe_bytes)} bytes) ###\n".encode())
    f.write(probe_bytes)
    if not probe_bytes.endswith(b"\n"):
        f.write(b"\n")
    f.write(f"### RESPONSE ({len(response)} bytes) ###\n".encode())
    f.write(response)
    if not response.endswith(b"\n"):
        f.write(b"\n")
    f.write(b"### END ###\n")

print(f"hello={len(hello)} probe={len(probe_bytes)} response={len(response)}",
      file=sys.stderr)
PYEOF

run_probe() {
    local label="$1"
    local envelope="$2"
    echo "→ Probe '$label'" >&2
    python3 "$PROBE_HELPER" "$CAR_IP" "$SAI_PORT" "$envelope" "$OUTDIR/probe-$label.txt"
    sleep 1
}

run_probe "01-getservices" '<Service><GetServices/></Service>\n'
run_probe "02-status-get"  '<Status><Get/></Status>\n'
run_probe "03-query"       '<Query type="services"/>\n'
run_probe "04-xml-prolog"  '<?xml version="1.0"?>\n<Service><GetServices/></Service>\n'

wait "$TCPDUMP_PID" 2>/dev/null || true

echo
echo "Done. Artifacts in $OUTDIR:"
ls -la "$OUTDIR"
