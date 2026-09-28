#!/usr/bin/env bash
# Install x11vnc on the Pi. We run it via mlpi-vnc.service (in systemd/), bound to
# 192.168.7.2 so the laptop/Wi-Fi side cannot reach the screen.
#
# x11vnc was chosen over wayvnc because Pi OS Bookworm on Pi Zero 2W still defaults
# to X11 (labwc/Wayland is too heavy for 512 MB RAM). When/if we migrate to a CCC-
# RFB-Extension-aware server, this script becomes the install hook.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root." >&2
    exit 1
fi

if ! command -v x11vnc >/dev/null 2>&1; then
    echo "Installing x11vnc..." >&2
    apt-get update
    apt-get install -y x11vnc
fi

echo "x11vnc installed: $(x11vnc -version 2>&1 | head -1)"
