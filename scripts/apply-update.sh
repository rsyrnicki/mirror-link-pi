#!/usr/bin/env bash
# Runs ON THE PI (as root), started by scripts/update-pi.sh from the laptop.
# Usage: apply-update.sh <unpacked-dir> <reboot 0|1> [VERSION lines...]

set -euo pipefail

SRC="$1"; REBOOT="${2:-1}"; shift 2 || true
# Overridable for the tests only.
OPT="${MLPI_OPT:-/opt/mlpi}"
SYSD="${MLPI_SYSTEMD_DIR:-/etc/systemd/system}"
TOML="${MLPI_TOML:-/boot/firmware/mlpi.toml}"
SYSTEMCTL="${MLPI_SYSTEMCTL:-systemctl}"

echo "==> installing into $OPT"
rm -rf "$OPT.new"
cp -r "$SRC" "$OPT.new"
chown -R 0:0 "$OPT.new" 2>/dev/null || true    # unpacked as the Pi user
if [[ ! -s "$OPT.new/vendor/scrcpy-server" && -d "$OPT/vendor" ]]; then
    cp -a "$OPT/vendor" "$OPT.new/"           # no fresh copy came along: keep the old one
fi
# The scrcpy server must match the pinned checksum: an update cut short by a power
# cut once left it empty, and the phone then only answers "Aborted".
want="$(sed -n 's/^SHA256="\([0-9a-f]*\)"$/\1/p' "$OPT.new/scripts/fetch-scrcpy-server.sh" 2>/dev/null || true)"
jar="$OPT.new/vendor/scrcpy-server"
if [[ -n "$want" && -e "$jar" ]] && ! echo "$want  $jar" | sha256sum -c --status; then
    echo "WARNING: $OPT/vendor/scrcpy-server is damaged (checksum mismatch): phone mode"
    echo "         won't start. Run ./scripts/update-pi.sh from a laptop with internet"
    echo "         access, which brings a verified copy."
fi
printf '%s\n' "$@" > "$OPT.new/VERSION"
rm -rf "$OPT.old"
[[ -d "$OPT" ]] && mv "$OPT" "$OPT.old"
mv "$OPT.new" "$OPT"
rm -rf "$OPT.old" "$SRC"
sync      # on the card now: a power cut right after an update must not leave empty files

echo "==> systemd units"
install -m 0644 "$OPT"/systemd/*.service "$OPT"/systemd/mlpi.target "$SYSD/"
install -d "$SYSD/sysinit.target.wants"
ln -sfn /etc/systemd/system/mlpi-bootcheck.service "$SYSD/sysinit.target.wants/mlpi-bootcheck.service"
$SYSTEMCTL daemon-reload

# What prepare-sd.sh --phone installs; it can't be installed from here (no internet).
missing=()
phone="$(awk '/^\[/{s=$0} s=="[phone]" && /^enabled *= *true/{print "on"}' "$TOML" 2>/dev/null || true)"
if [[ "$phone" == on ]]; then
    for bin in adb iw; do
        command -v "$bin" >/dev/null || missing+=("$bin")
    done
    compgen -G '/usr/lib/*/libavcodec.so.*' >/dev/null || missing+=(libavcodec)
fi
if (( ${#missing[@]} )); then
    echo "NOTE: phone mode needs packages this card doesn't have: ${missing[*]}"
    echo "      Run once on the laptop: sudo ./scripts/prepare-sd.sh --phone /dev/sdX"
fi

sync
cat "$OPT/VERSION"
if [[ "$REBOOT" == 1 ]]; then
    echo "==> rebooting (about 30 s)"
    systemd-run --on-active=2 --quiet systemctl reboot
else
    echo "==> restarting MirrorLink-Pi"
    $SYSTEMCTL restart mlpi.target
fi
