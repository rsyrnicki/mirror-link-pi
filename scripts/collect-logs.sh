#!/usr/bin/env bash
# Copy the recorded sessions off a MirrorLink-Pi SD card and summarise them.
# Runs on the LAPTOP (Linux).
#
# Usage:
#   sudo ./scripts/collect-logs.sh /dev/sdX [dest-dir]      # the SD card device
#   sudo ./scripts/collect-logs.sh --root DIR [dest-dir]    # rootfs already mounted
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

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root) ROOT="$2"; shift 2 ;;
        /dev/*) DEV="$1"; shift ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) DEST="$1"; shift ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
[[ -n "$DEV" || -n "$ROOT" ]] || die "give the SD card device (/dev/sdX) or --root DIR"

cleanup() {
    if [[ -n "$MNT" ]]; then
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

SRC="$ROOT/var/lib/mlpi"
if (( ${DATA_LAYOUT:-0} )); then
    SRC="$ROOT"                   # the data partition is mounted at /var/lib/mlpi
fi
[[ -d "$SRC/sessions" ]] || die "no $SRC/sessions on the card — did MirrorLink-Pi ever run?"

DEST="${DEST:-$PWD/car-logs/$(date +%Y-%m-%d_%H%M%S)}"
mkdir -p "$DEST"
cp -a "$SRC/sessions" "$DEST/"
rm -f "$DEST/sessions/current"
cp -a "$SRC/boot-count" "$SRC/winner-variant" "$DEST/" 2>/dev/null || true
if [[ -d "$ROOT/var/log/journal" ]]; then
    cp -a "$ROOT/var/log/journal" "$DEST/journal"
fi

PYTHONPATH="$REPO/src" python3 -m mlpi report "$DEST"/sessions/[0-9]* > "$DEST/REPORT.txt" \
    || echo "report generation failed (the raw data is still there)" >&2

# One upload-sized zip per session (the full set is often too big to send).
mkdir -p "$DEST/zips"
python3 - "$DEST" "$REPO/src" <<'PY'
import subprocess, sys, zipfile
from pathlib import Path
dest, src = Path(sys.argv[1]), sys.argv[2]
LIMIT = 20_000_000                      # bytes per file in the upload zips
sessions = sorted(p for p in (dest / "sessions").iterdir() if p.is_dir() and p.name.isdigit())
for s in sessions:
    report = subprocess.run([sys.executable, "-m", "mlpi", "report", str(s)], capture_output=True,
                            text=True, env={"PYTHONPATH": src}).stdout
    out = dest / "zips" / f"session-{s.name}.zip"
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
if sessions:
    latest = dest / "zips" / f"session-{sessions[-1].name}.zip"
    (dest / "zips" / "latest.zip").write_bytes(latest.read_bytes())
PY

if [[ -n "${SUDO_USER:-}" ]]; then
    chown -R "$SUDO_USER": "$DEST"
fi

echo "copied $(ls "$DEST/sessions" | wc -l) session(s) to $DEST"
echo "summary: $DEST/REPORT.txt"
echo "to send one session: $DEST/zips/latest.zip (or zips/session-NNNN.zip)"
