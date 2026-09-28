#!/usr/bin/env bash
# Install dnsmasq config for the usb0 subnet so the head unit gets a DHCP lease.
# Does NOT enable the system-wide dnsmasq.service — the dnsmasq-usb0.service unit
# (in systemd/) runs an isolated instance bound to usb0 only.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root." >&2
    exit 1
fi

if ! command -v dnsmasq >/dev/null 2>&1; then
    echo "Installing dnsmasq..." >&2
    apt-get update
    apt-get install -y dnsmasq
fi

# Disable the stock service — we run our own instance via mlpi systemd units.
systemctl disable --now dnsmasq.service 2>/dev/null || true

install -d -m 0755 /etc/dnsmasq.d

cat > /etc/dnsmasq.d/usb0.conf <<'EOF'
# MirrorLink-Pi: DHCP for the head unit on usb0.
# Bound only to usb0 — does NOT clobber port 53 on the host's other interfaces.
interface=usb0
bind-interfaces
except-interface=lo
no-resolv
no-hosts

dhcp-range=192.168.7.10,192.168.7.50,255.255.255.0,1h
dhcp-option=option:router,192.168.7.2
dhcp-option=option:dns-server,192.168.7.2
dhcp-authoritative
log-dhcp
log-queries
EOF

echo "Wrote /etc/dnsmasq.d/usb0.conf"
