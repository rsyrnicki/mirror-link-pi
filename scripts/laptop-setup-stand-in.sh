#!/usr/bin/env bash
# Laptop-side: configure a USB-Ethernet adapter to stand in for the Pi's usb0.
# Use for development on a machine that cannot do USB gadget mode itself.
#
# Find the adapter name with:  nmcli -t -f DEVICE,TYPE device | grep ethernet
# Then run:                    sudo ./scripts/laptop-setup-stand-in.sh <iface>

set -euo pipefail

IFACE="${1:-}"
if [[ -z "$IFACE" ]]; then
    echo "Usage: sudo $0 <iface-name>" >&2
    echo "Tip: nmcli -t -f DEVICE,TYPE device | grep ethernet" >&2
    exit 2
fi

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (try: sudo $0 $IFACE)" >&2
    exit 1
fi

if ! ip link show "$IFACE" &>/dev/null; then
    echo "Interface $IFACE does not exist." >&2
    exit 1
fi

# Tell NetworkManager not to manage this interface — otherwise it overrides our IP.
if command -v nmcli >/dev/null 2>&1; then
    nmcli device set "$IFACE" managed no || true
fi

ip addr flush dev "$IFACE"
ip addr add 192.168.7.2/24 dev "$IFACE"
ip link set "$IFACE" up

# Multicast-routing gotcha: with WiFi up at a lower metric, SSDP packets to
# 239.255.255.250 may go out the wrong NIC. Force the multicast block via this iface.
ip route replace 239.0.0.0/8 dev "$IFACE"

cat <<EOF
$IFACE configured as usb0 stand-in:
  IP:           192.168.7.2/24
  Multicast:    239.0.0.0/8 routed via $IFACE
  NM:           unmanaged

Next:
  MLPI_INTERFACE=$IFACE python -m mlpi run --config config/mlpi.toml.example

To reset:
  ip addr flush dev $IFACE && nmcli device set $IFACE managed yes
EOF
