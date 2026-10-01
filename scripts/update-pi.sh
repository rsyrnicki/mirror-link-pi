#!/usr/bin/env bash
# Update the code on a running MirrorLink-Pi over the USB cable, without taking the
# SD card out. Runs on the LAPTOP, as your normal user (not sudo).
#
# Usage:
#   ./scripts/update-pi.sh <pi-user>@192.168.7.2       # Pi on this laptop's USB port
#   ./scripts/update-pi.sh --no-reboot <pi-user>@<ip>
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
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-reboot) REBOOT=0; shift ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) TARGET="$1"; shift ;;
    esac
done
[[ -n "$TARGET" ]] || { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
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
        src config systemd scripts docs README.md LICENSE pyproject.toml
} | "${SSH[@]}" "$TARGET" \
    'rm -rf /tmp/mlpi-update && mkdir -p /tmp/mlpi-update && tar -C /tmp/mlpi-update -xf -' \
    || { echo "ERROR: couldn't reach $TARGET (Pi plugged in? LED blinking twice? if the card"
         echo "       was re-flashed: rm $KNOWN)"; exit 1; } >&2

VERSION="prepared: $(date -u +%FT%TZ) (update-pi.sh)"
GIT="$(git -C "$REPO" describe --always --dirty --tags 2>/dev/null || true)"
echo "==> installing (the Pi asks for your password only if its sudo needs one)"
"${SSH[@]}" -t "$TARGET" sudo /bin/bash /tmp/mlpi-update/scripts/apply-update.sh \
    /tmp/mlpi-update "$REBOOT" "'$VERSION'" "'git: $GIT'"
