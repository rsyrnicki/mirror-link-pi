#!/usr/bin/env bash
# Configure a USB-NCM (or ECM/RNDIS) gadget on the Pi via libcomposite/configfs.
# Idempotent: deletes any existing g_mlpi gadget first, then re-creates it.
#
# Env overrides:
#   MLPI_USB_FUNC   ncm (default), ecm, or rndis
#   MLPI_USB_VID    USB Vendor ID  (default 0x1d6b — Linux Foundation)
#   MLPI_USB_PID    USB Product ID (default 0x0104 — Multifunction Composite)
#   MLPI_USB_ADDR   IPv4 to assign to usb0 (default 192.168.7.2/24)
#
# Some MirrorLink head units filter the USB descriptor against a whitelist of
# certified vendors (Samsung 0x04e8, HTC 0x0bb4, LG 0x1004). If the car ignores
# our default IDs, set MLPI_USB_VID/PID and re-run — see docs/known-gaps.md.

set -euo pipefail

CFG_ROOT=/sys/kernel/config/usb_gadget
G="$CFG_ROOT/g_mlpi"
FUNC="${MLPI_USB_FUNC:-ncm}"
VID="${MLPI_USB_VID:-0x1d6b}"
PID="${MLPI_USB_PID:-0x0104}"
ADDR="${MLPI_USB_ADDR:-192.168.7.2/24}"

case "$FUNC" in
    ncm|ecm|rndis) ;;
    *) echo "MLPI_USB_FUNC must be ncm, ecm, or rndis (got: $FUNC)" >&2; exit 2 ;;
esac

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root." >&2
    exit 1
fi

# 1. Make sure libcomposite is available and configfs is mounted.
modprobe libcomposite
[[ -d "$CFG_ROOT" ]] || { echo "configfs not mounted at $CFG_ROOT" >&2; exit 1; }

# 2. Cleanup any prior g_mlpi instance (idempotent re-runs).
if [[ -d "$G" ]]; then
    echo "" > "$G/UDC" 2>/dev/null || true
    # Unlink function from configs first, then remove dirs in reverse order.
    find "$G/configs" -mindepth 2 -maxdepth 2 -type l -delete 2>/dev/null || true
    rmdir "$G"/configs/*/strings/* 2>/dev/null || true
    rmdir "$G"/configs/* 2>/dev/null || true
    rmdir "$G"/functions/* 2>/dev/null || true
    rmdir "$G"/strings/* 2>/dev/null || true
    rmdir "$G" 2>/dev/null || true
fi

# 3. Build the gadget tree.
mkdir -p "$G"
echo "$VID" > "$G/idVendor"
echo "$PID" > "$G/idProduct"
echo 0x0100 > "$G/bcdDevice"
echo 0x0200 > "$G/bcdUSB"

mkdir -p "$G/strings/0x409"
echo "0123456789" > "$G/strings/0x409/serialnumber"
echo "MirrorLink Pi" > "$G/strings/0x409/manufacturer"
echo "MLPI Gadget" > "$G/strings/0x409/product"

mkdir -p "$G/configs/c.1/strings/0x409"
echo "MLPI Config" > "$G/configs/c.1/strings/0x409/configuration"
echo 250 > "$G/configs/c.1/MaxPower"

mkdir -p "$G/functions/${FUNC}.usb0"
# Deterministic MAC addresses keep DHCP leases stable across reboots.
echo "02:1a:11:00:00:01" > "$G/functions/${FUNC}.usb0/host_addr"
echo "02:1a:11:00:00:02" > "$G/functions/${FUNC}.usb0/dev_addr"

ln -s "$G/functions/${FUNC}.usb0" "$G/configs/c.1/"

# 4. Bind to the first available USB Device Controller.
UDC=$(ls /sys/class/udc 2>/dev/null | head -1 || true)
if [[ -z "$UDC" ]]; then
    echo "No UDC available — is dwc2 enabled in /boot/firmware/config.txt and dwc2 module loaded?" >&2
    exit 1
fi
echo "$UDC" > "$G/UDC"
echo "Gadget bound to UDC: $UDC (function=$FUNC, VID=$VID, PID=$PID)"

# 5. Wait for usb0 to appear, then assign the static IP.
for _ in {1..40}; do
    ip link show usb0 &>/dev/null && break
    sleep 0.25
done
if ! ip link show usb0 &>/dev/null; then
    echo "Gadget bound but usb0 did not appear" >&2
    exit 1
fi

# Flush + add — idempotent. ``|| true`` on add covers the "already there" case.
ip addr flush dev usb0
ip addr add "$ADDR" dev usb0
ip link set usb0 up
echo "usb0 configured: $ADDR"
