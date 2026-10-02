#!/usr/bin/env bash
# A new MirrorLink-Pi SD card in one command: downloads Raspberry Pi OS Lite (64-bit),
# checks it, writes it to the card and sets it up (prepare-sd.sh). No Raspberry Pi
# Imager and none of its settings needed. Runs on the LAPTOP (Linux) with sudo.
#
# Usage:
#   sudo ./scripts/install-sd.sh /dev/sdX                   # erases the whole card
#   sudo ./scripts/install-sd.sh /dev/sdX --password secret # prepare-sd.sh options
#   --wifi-password PW   keep the hotspot password your phone already has saved
#   --image FILE   use a downloaded .img or .img.xz instead of the latest release
#   --no-phone     leave out phone mode       --yes   don't ask before erasing
#
# Defaults passed to prepare-sd.sh: --phone --ssh --data-partition, and the login
# user mlpi / password mlpi / hostname mlpi. Pair the phone first (mlpi pair-phone)
# so its key goes onto the card.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LATEST="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
DEV=""
IMAGE=""
YES=0
PHONE=(--phone)
EXTRA=()

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*"; }
usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        /dev/*) DEV="$1"; shift ;;
        --image) IMAGE="$2"; shift 2 ;;
        --no-phone) PHONE=(); shift ;;
        --yes) YES=1; shift ;;
        -h|--help) usage ;;
        --user|--password|--hostname|--wifi-password) EXTRA+=("$1" "$2"); shift 2 ;;
        *) EXTRA+=("$1"); shift ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run as root: sudo $0 ..."
[[ -n "$DEV" ]] || usage
[[ -b "$DEV" ]] || die "$DEV is not a block device"
ROOT_SRC="$(findmnt -no SOURCE / || true)"
if [[ -n "$ROOT_SRC" ]] && lsblk -lnpo NAME "$DEV" | grep -qx "$ROOT_SRC"; then
    die "$DEV holds this laptop's system — wrong device!"
fi

# ---------- the image: latest Raspberry Pi OS Lite (64-bit), checksum-verified ----------

owner="${SUDO_USER:-root}"
cache="$(getent passwd "$owner" | cut -d: -f6)/.cache/mlpi/images"
if [[ -z "$IMAGE" ]]; then
    url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$LATEST")" \
        || die "can't reach downloads.raspberrypi.com (internet?)"
    IMAGE="$cache/$(basename "$url")"
    sudo -u "$owner" mkdir -p "$cache"
    expected="$(curl -fsSL "$url.sha256" | awk '{print $1}')" || die "no checksum for $url"
    if [[ -f "$IMAGE" ]] && echo "$expected  $IMAGE" | sha256sum -c --status; then
        say "using the downloaded $(basename "$IMAGE")"
    else
        say "downloading $(basename "$url") (about 500 MB)"
        sudo -u "$owner" curl -fL --progress-bar -o "$IMAGE.part" "$url"
        echo "$expected  $IMAGE.part" | sha256sum -c --status \
            || { rm -f "$IMAGE.part"; die "download damaged (checksum mismatch); try again"; }
        mv "$IMAGE.part" "$IMAGE"
    fi
fi
[[ -f "$IMAGE" ]] || die "no image $IMAGE"

# ---------- erase the card and write the image ----------

info="$(lsblk -dno MODEL,SIZE "$DEV" | tr -s ' ')"
echo
echo "    $DEV: ${info:-unknown}"
lsblk -no NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEV" | sed 's/^/    /'
echo
if (( ! YES )); then
    read -r -p "Erase $DEV and install MirrorLink-Pi on it? Type 'yes': " answer
    [[ "$answer" == yes ]] || die "cancelled"
fi
while read -r mp; do
    [[ -n "$mp" ]] && umount "$mp"
done < <(lsblk -lno MOUNTPOINT "$DEV")

say "writing $(basename "$IMAGE") to $DEV"
if [[ "$IMAGE" == *.xz ]]; then
    xz -dc "$IMAGE" | dd of="$DEV" bs=4M iflag=fullblock conv=fsync status=progress
else
    dd if="$IMAGE" of="$DEV" bs=4M conv=fsync status=progress
fi
sync
partx -u "$DEV" 2>/dev/null || blockdev --rereadpt "$DEV" 2>/dev/null || true
udevadm settle 2>/dev/null || sleep 2

# ---------- set it up ----------

"${MLPI_PREPARE_SD:-$REPO/scripts/prepare-sd.sh}" "${PHONE[@]}" --ssh --data-partition \
    "${EXTRA[@]}" "$DEV"
