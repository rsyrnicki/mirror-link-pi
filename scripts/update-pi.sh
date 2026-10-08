#!/usr/bin/env bash
# Update the code on a running MirrorLink-Pi over the USB cable, without taking the
# SD card out. Runs on the LAPTOP, as your normal user (not sudo).
#
# Usage:
#   ./scripts/update-pi.sh                         # mlpi@192.168.7.2: Pi on this laptop's USB
#   ./scripts/update-pi.sh --no-reboot <user>@<ip>
#   sudo ./scripts/update-pi.sh --card /dev/sdX    # card in the laptop (Pi unreachable)
#
# Needs a card prepared once with `prepare-sd.sh --ssh` (key login with the key in
# ~/.config/mlpi/ssh/). Copies src, config, systemd units, scripts and docs, keeps the
# scrcpy server and all recordings, then reboots the Pi (~30 s). New system packages
# can't be installed this way: if a version needs one, the Pi says so and
# `sudo ./scripts/prepare-sd.sh --phone /dev/sdX` does it.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
REBOOT=1
TARGET=""
CARD=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-reboot) REBOOT=0; shift ;;
        --card) CARD="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) TARGET="$1"; shift ;;
    esac
done
# A verified scrcpy server goes along with every update and replaces the Pi's copy
# (it can't be downloaded there: no internet). Without one, the Pi keeps its own.
JAR=()
if "$REPO/scripts/fetch-scrcpy-server.sh" >/dev/null 2>&1; then    # verifies; downloads if needed
    JAR=(vendor/scrcpy-server)
else
    echo "NOTE: couldn't get the scrcpy server (no internet?): the Pi keeps its own copy" >&2
fi

VERSION="prepared: $(date -u +%FT%TZ) (update-pi.sh)"
GIT="$(git -C "$REPO" describe --always --dirty --tags 2>/dev/null || true)"

if [[ -n "$CARD" ]]; then
    # The same installation, onto the card's root partition mounted on this laptop.
    # (Its check for missing packages is skipped: it would look at the laptop.)
    [[ $EUID -eq 0 ]] || { echo "ERROR: --card needs root: sudo $0 --card $CARD" >&2; exit 1; }
    MNTS=()
    trap 'for m in "${MNTS[@]}"; do umount "$m" 2>/dev/null; rmdir "$m" 2>/dev/null; done' EXIT
    mount_part() {   # mount_part PARTITION → sets M ("" if it can't be mounted)
        local part="$1"
        M="$(lsblk -lno MOUNTPOINT "$part" | head -1)"
        if [[ -z "$M" ]]; then
            M="$(mktemp -d /tmp/mlpi-card.XXXX)"
            if mount "$part" "$M"; then MNTS+=("$M"); else rmdir "$M"; M=""; fi
        fi
    }
    rootpart=""
    while read -r part kind; do             # blkid probes directly (no udev needed)
        [[ "$kind" == part ]] || continue
        fs="$(blkid -p -o value -s TYPE "$part" 2>/dev/null || true)"
        label="$(blkid -p -o value -s LABEL "$part" 2>/dev/null || true)"
        if [[ "$fs" == ext4 && "$label" != mlpi-data && -z "$rootpart" ]]; then rootpart="$part"; fi
    done < <(lsblk -lnpo NAME,TYPE "$CARD")
    [[ -n "$rootpart" ]] || { echo "ERROR: no Raspberry Pi OS card at $CARD" >&2; exit 1; }
    mount_part "$rootpart"; ROOTMNT="$M"
    [[ -n "$ROOTMNT" ]] || { echo "ERROR: couldn't mount $rootpart" >&2; exit 1; }
    [[ -d "$ROOTMNT/opt/mlpi" ]] || echo "NOTE: no /opt/mlpi on the card yet; installing anyway"
    tmp="$(mktemp -d /tmp/mlpi-update.XXXX)"
    tar -C "$REPO" --exclude='__pycache__' --exclude='*.pyc' -cf - \
        src config systemd scripts docs README.md LICENSE pyproject.toml "${JAR[@]}" | tar -C "$tmp" -xf -
    echo "==> installing onto the card ($rootpart)"
    MLPI_OPT="$ROOTMNT/opt/mlpi" MLPI_SYSTEMD_DIR="$ROOTMNT/etc/systemd/system" \
        MLPI_TOML=/nonexistent MLPI_SYSTEMCTL=true \
        bash "$tmp/scripts/apply-update.sh" "$tmp" 0 "$VERSION (card)" "git: $GIT"
    sync
    echo "==> done: put the card back into the Pi"
    exit 0
fi

TARGET="${TARGET:-mlpi}"
[[ "$TARGET" == *@* ]] || TARGET="$TARGET@192.168.7.2"

KEY="${MLPI_SSH_KEY:-$HOME/.config/mlpi/ssh/id_ed25519}"
[[ -f "$KEY" ]] || { echo "ERROR: no update key at $KEY — prepare the card once with --ssh" >&2; exit 1; }
# A re-flashed card gets new host keys; this file only ever holds the Pi's.
KNOWN="$HOME/.config/mlpi/ssh/known_hosts"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
     -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=10)

echo "==> copying the code to $TARGET"
{
    tar -C "$REPO" --exclude='__pycache__' --exclude='*.pyc' -cf - \
        src config systemd scripts docs README.md LICENSE pyproject.toml "${JAR[@]}"
} | "${SSH[@]}" "$TARGET" \
    'rm -rf /tmp/mlpi-update && mkdir -p /tmp/mlpi-update && tar -C /tmp/mlpi-update -xf -' \
    || { echo "ERROR: couldn't reach $TARGET (Pi plugged in? LED blinking twice? if the card"
         echo "       was re-flashed: rm $KNOWN)"; exit 1; } >&2

echo "==> installing (the Pi asks for your password only if its sudo needs one)"
"${SSH[@]}" -t "$TARGET" sudo /bin/bash /tmp/mlpi-update/scripts/apply-update.sh \
    /tmp/mlpi-update "$REBOOT" "'$VERSION'" "'git: $GIT'"
