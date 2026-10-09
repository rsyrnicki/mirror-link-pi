#!/usr/bin/env bash
# Copy the recorded sessions off a MirrorLink-Pi and summarise them.
# Runs on the LAPTOP (Linux).
#
# Usage:
#   ./scripts/collect-logs.sh --pi [user@host] [dest-dir]   # over the USB cable (SSH),
#                                                           # card stays in the Pi
#   sudo ./scripts/collect-logs.sh /dev/sdX [dest-dir]      # the SD card device
#   sudo ./scripts/collect-logs.sh --root DIR [dest-dir]    # rootfs already mounted
#
# --pi needs a card prepared with --ssh (install-sd.sh does that); default target
# mlpi@192.168.7.2, the Pi plugged into the laptop's USB port as for update-pi.sh.
#
# Result: dest-dir (default ./car-logs/<timestamp>) containing
#   sessions/NNNN/...   one directory per Pi boot (events, pcap, journal, VNC dumps)
#   journal/            the persistent systemd journal (readable with journalctl -D)
#   REPORT.txt          `mlpi report` over every session
#   zips/session-NNNN.zip  one zip per session (its files + its own REPORT.txt),
#   zips/latest.zip        small enough to upload; latest = the most recent boot
#                          (files over 20 MB, i.e. big pcaps, are left out of the zips)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV=""
ROOT=""
DEST=""
MNT=""
PI=""
EXTRA_MNTS=()

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root) ROOT="$2"; shift 2 ;;
        --pi)
            PI="mlpi"
            if [[ $# -gt 1 && "$2" != */* && "$2" != -* ]]; then PI="$2"; shift; fi
            shift ;;
        /dev/*) DEV="$1"; shift ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) DEST="$1"; shift ;;
    esac
done
if [[ -z "$PI" ]]; then
    [[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
    [[ -n "$DEV" || -n "$ROOT" ]] || die "give --pi, the SD card device (/dev/sdX) or --root DIR"
fi

cleanup() {
    for m in "${EXTRA_MNTS[@]}"; do
        umount "$m" 2>/dev/null || true
        rmdir "$m" 2>/dev/null || true
    done
    if [[ -n "$PI" && -n "$MNT" ]]; then
        rm -rf "$MNT"
    elif [[ -n "$MNT" ]]; then
        umount "$MNT" 2>/dev/null || true
        rmdir "$MNT" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [[ -n "$DEV" ]]; then
    # Cards prepared with --data-partition keep the recordings on "mlpi-data".
    PART="$(lsblk -lnpo NAME,LABEL "$DEV" | awk '$2=="mlpi-data"{print $1; exit}')"
    DATA_LAYOUT=0
    if [[ -n "$PART" ]]; then
        DATA_LAYOUT=1
    else
        PART="$(lsblk -lnpo NAME,FSTYPE "$DEV" | awk '$2=="ext4"{print $1; exit}')"
    fi
    [[ -n "$PART" ]] || die "no ext4 partition on $DEV"
    EXISTING="$(lsblk -lno MOUNTPOINT "$PART" | head -1)"
    if [[ -n "$EXISTING" ]]; then
        ROOT="$EXISTING"
    else
        MNT="$(mktemp -d /tmp/mlpi-root.XXXX)"
        mount -o ro "$PART" "$MNT"
        ROOT="$MNT"
    fi
fi

if [[ -n "$PI" ]]; then
    [[ "$PI" == *@* ]] || PI="$PI@192.168.7.2"
    KEY="${MLPI_SSH_KEY:-$HOME/.config/mlpi/ssh/id_ed25519}"
    [[ -f "$KEY" ]] || die "no SSH key at $KEY: the card needs prepare-sd.sh --ssh (install-sd.sh does it)"
    MNT="$(mktemp -d /tmp/mlpi-logs.XXXX)"
    echo "==> copying the sessions from $PI"
    SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
         -o UserKnownHostsFile="$HOME/.config/mlpi/ssh/known_hosts" -o ConnectTimeout=10 "$PI")
    "${SSH[@]}" 'sudo tar -C /var/lib/mlpi -cf - --ignore-failed-read sessions boot-count winner-variant 2>/dev/null' \
        | tar -C "$MNT" -xf - \
        || die "couldn't copy from $PI (Pi plugged into the laptop? LED blinking twice?)"
    # Sessions that ended up on the root fs (data partition not mounted at the time)
    # sit under the mount point: reach them through a bind mount of /.
    mkdir -p "$MNT/.rootfs-sessions"
    "${SSH[@]}" 'mountpoint -q /var/lib/mlpi && sudo sh -c '"'"'d=$(mktemp -d) && mount --bind / "$d" && tar -C "$d/var/lib/mlpi" -cf - sessions 2>/dev/null; umount "$d"; rmdir "$d"'"'"'' \
        2>/dev/null | tar -C "$MNT/.rootfs-sessions" -xf - 2>/dev/null || true
    "${SSH[@]}" 'cat /boot/firmware/mlpi-boot-problem.txt 2>/dev/null' \
        > "$MNT/.mlpi-boot-problem.txt" 2>/dev/null || true
    SRC="$MNT"
    ROOT="$MNT"
else
    SRC="$ROOT/var/lib/mlpi"
fi
if (( ${DATA_LAYOUT:-0} )); then
    SRC="$ROOT"                   # the data partition is mounted at /var/lib/mlpi
fi
[[ -d "$SRC/sessions" ]] || die "no $SRC/sessions on the card — did MirrorLink-Pi ever run?"

DEST="${DEST:-$PWD/car-logs/$(date +%Y-%m-%d_%H%M%S)}"
mkdir -p "$DEST"
cp -a "$SRC/sessions" "$DEST/"
# With --pi the Pi is running on this laptop right now: its newest session is this
# laptop start, not the drive. Remember it, so latest.zip can skip it.
RUNNING=""
if [[ -n "$PI" && -L "$DEST/sessions/current" ]]; then
    RUNNING="$(basename "$(readlink "$DEST/sessions/current")")"
fi
rm -f "$DEST/sessions/current"
cp -a "$SRC/boot-count" "$SRC/winner-variant" "$DEST/" 2>/dev/null || true

# Card with a data partition: also the root fs (sessions written while the data
# partition wasn't mounted) and the boot partition (mlpi-boot-problem.txt).
MOUNTED=""
mount_ro() {   # mount_ro PARTITION → sets MOUNTED to where it is mounted ("" = failed)
    MOUNTED="$(lsblk -lno MOUNTPOINT "$1" | head -1)"
    [[ -n "$MOUNTED" ]] && return
    local m; m="$(mktemp -d /tmp/mlpi-part.XXXX)"
    if mount -o ro "$1" "$m"; then EXTRA_MNTS+=("$m"); MOUNTED="$m"; else rmdir "$m"; fi
}
if [[ -n "$DEV" ]] && (( ${DATA_LAYOUT:-0} )); then
    rootpart="$(lsblk -lnpo NAME,FSTYPE,LABEL "$DEV" | awk '$2=="ext4" && $3!="mlpi-data"{print $1; exit}')"
    bootpart="$(lsblk -lnpo NAME,FSTYPE "$DEV" | awk '$2=="vfat"{print $1; exit}')"
    if [[ -n "$rootpart" ]]; then
        mount_ro "$rootpart"
        if [[ -n "$MOUNTED" && -d "$MOUNTED/var/lib/mlpi/sessions" ]]; then
            mkdir -p "$DEST/rootfs-sessions"
            cp -a "$MOUNTED/var/lib/mlpi/sessions/." "$DEST/rootfs-sessions/"
        fi
    fi
    if [[ -n "$bootpart" ]]; then
        mount_ro "$bootpart"
        if [[ -n "$MOUNTED" && -f "$MOUNTED/mlpi-boot-problem.txt" ]]; then
            cp "$MOUNTED/mlpi-boot-problem.txt" "$DEST/"
        fi
    fi
fi
if [[ -n "$PI" ]]; then
    if compgen -G "$MNT/.rootfs-sessions/sessions/[0-9]*" >/dev/null; then
        mkdir -p "$DEST/rootfs-sessions" && cp -a "$MNT/.rootfs-sessions/sessions/." "$DEST/rootfs-sessions/"
    fi
    if [[ -s "$MNT/.mlpi-boot-problem.txt" ]]; then
        cp "$MNT/.mlpi-boot-problem.txt" "$DEST/mlpi-boot-problem.txt"
    fi
fi
rm -f "$DEST/rootfs-sessions/current"
if [[ -d "$ROOT/var/log/journal" ]]; then
    cp -a "$ROOT/var/log/journal" "$DEST/journal"
fi

PYTHONPATH="$REPO/src" python3 -m mlpi report "$DEST"/sessions/[0-9]* > "$DEST/REPORT.txt" \
    || echo "report generation failed (the raw data is still there)" >&2

# One upload-sized zip per session (the full set is often too big to send).
mkdir -p "$DEST/zips"
python3 - "$DEST" "$REPO/src" "$RUNNING" <<'PY'
import subprocess, sys, zipfile
from pathlib import Path
dest, src = Path(sys.argv[1]), sys.argv[2]
running = sys.argv[3] if len(sys.argv) > 3 else ""   # --pi: the session of this very start
LIMIT = 20_000_000                      # bytes per file in the upload zips
sessions = sorted(p for p in (dest / "sessions").iterdir() if p.is_dir() and p.name.isdigit())
rootfs = dest / "rootfs-sessions"     # written while the data partition wasn't mounted
extra = sorted(p for p in rootfs.iterdir() if p.is_dir() and p.name.isdigit()) \
    if rootfs.is_dir() else []
for s in sessions + extra:
    report = subprocess.run([sys.executable, "-m", "mlpi", "report", str(s)], capture_output=True,
                            text=True, env={"PYTHONPATH": src}).stdout
    prefix = "rootfs-session" if s.parent == rootfs else "session"
    out = dest / "zips" / f"{prefix}-{s.name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(f"{s.name}/REPORT.txt", report)
        skipped = []
        for f in sorted(s.rglob("*")):
            if not f.is_file() or f.name == "REPORT.txt":
                continue
            if f.stat().st_size > LIMIT:        # old cards: rotating full-video pcaps
                skipped.append(f"{f.relative_to(s)} ({f.stat().st_size / 1e6:.0f} MB)")
                continue
            z.write(f, f"{s.name}/{f.relative_to(s)}")
        if skipped:
            z.writestr(f"{s.name}/SKIPPED.txt", "left out of this zip (still in sessions/):\n"
                       + "".join(f"  {x}\n" for x in skipped))
    print(f"  {out.name}: {out.stat().st_size / 1e6:.1f} MB")
earlier = [x for x in sessions if x.name != running]
if earlier:
    pick = earlier[-1]
    latest = dest / "zips" / f"session-{pick.name}.zip"
    (dest / "zips" / "latest.zip").write_bytes(latest.read_bytes())
    (dest / "zips" / "LATEST-IS.txt").write_text(pick.name + "\n")
PY

if [[ -n "${SUDO_USER:-}" ]]; then
    chown -R "$SUDO_USER": "$DEST"
fi

echo "copied $(ls "$DEST/sessions" | wc -l) session(s) to $DEST"
echo "summary: $DEST/REPORT.txt"
if [[ -f "$DEST/zips/LATEST-IS.txt" ]]; then
    echo "to send the last drive: $DEST/zips/latest.zip = session $(cat "$DEST/zips/LATEST-IS.txt")"
    [[ -n "$RUNNING" ]] && echo "  (session $RUNNING is this laptop start, left out of latest.zip)"
fi
echo "every session: $DEST/zips/session-NNNN.zip"
if [[ -d "$DEST/rootfs-sessions" ]]; then
    echo "NOTE: $(ls "$DEST/rootfs-sessions" | wc -l) session(s) were written while the data partition"
    echo "      wasn't mounted: zips/rootfs-session-NNNN.zip"
fi
if [[ -f "$DEST/mlpi-boot-problem.txt" ]]; then
    echo "NOTE: a start of MirrorLink-Pi failed: $DEST/mlpi-boot-problem.txt"
    head -1 "$DEST/mlpi-boot-problem.txt"
fi
