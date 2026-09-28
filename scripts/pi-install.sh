#!/usr/bin/env bash
# Top-level installer: run this once on a fresh Pi to set up MirrorLink-Pi.
# Calls the per-component setup scripts and installs systemd units + the python pkg.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (try: sudo $0)" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_PREFIX="${MLPI_PREFIX:-/opt/mlpi}"

echo "=== MirrorLink-Pi installer ==="
echo "Repo:    $REPO_ROOT"
echo "Prefix:  $INSTALL_PREFIX"

# 1. System packages
"$REPO_ROOT/scripts/pi-setup-vnc.sh"
"$REPO_ROOT/scripts/pi-setup-dnsmasq.sh"
apt-get install -y python3-venv

# 2. Copy repo + create venv
install -d -m 0755 "$INSTALL_PREFIX"
rsync -a --delete \
    --exclude '.git/' --exclude 'tests/' --exclude 'legacy/' --exclude '__pycache__/' \
    "$REPO_ROOT/" "$INSTALL_PREFIX/"

if [[ ! -d "$INSTALL_PREFIX/.venv" ]]; then
    python3 -m venv "$INSTALL_PREFIX/.venv"
fi
"$INSTALL_PREFIX/.venv/bin/pip" install --quiet -e "$INSTALL_PREFIX"

# 3. Production config (copied from example, edit as needed)
install -d -m 0755 /etc/mlpi
if [[ ! -f /etc/mlpi/mlpi.toml ]]; then
    install -m 0644 "$REPO_ROOT/config/mlpi.toml.example" /etc/mlpi/mlpi.toml
    echo "Wrote /etc/mlpi/mlpi.toml from example"
fi
install -d -m 0755 /var/log/mlpi

# 4. systemd units
install -m 0644 "$REPO_ROOT/systemd/"*.service /etc/systemd/system/
install -m 0644 "$REPO_ROOT/systemd/mlpi.target" /etc/systemd/system/
systemctl daemon-reload

# 5. Boot-time gadget setup needs dwc2 in /boot/firmware/config.txt
CFG_TXT=/boot/firmware/config.txt
if [[ -f "$CFG_TXT" ]] && ! grep -q '^dtoverlay=dwc2' "$CFG_TXT"; then
    echo "dtoverlay=dwc2,dr_mode=peripheral" >> "$CFG_TXT"
    echo "Appended dtoverlay=dwc2 to $CFG_TXT (reboot required)"
fi

cat <<EOF

=== Done ===
Next steps:
  1. Reboot the Pi if dtoverlay=dwc2 was just added.
  2. Plug the USB-OTG cable into the car.
  3. systemctl enable --now mlpi.target
  4. journalctl -u mlpi-upnp.service -f   # watch for SOAP requests from the head unit

Troubleshooting:
  - No usb0 after boot:       check 'ls /sys/class/udc' is non-empty
  - Head unit ignores us:     try MLPI_USB_VID/PID overrides — see docs/known-gaps.md
  - dnsmasq port-53 conflict: make sure systemd-resolved is bound only to 127.0.0.53
EOF
