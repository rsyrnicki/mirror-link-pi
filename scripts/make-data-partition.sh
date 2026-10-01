#!/usr/bin/env bash
# Called by prepare-sd.sh --data-partition. Runs on the LAPTOP, card not mounted.
# Usage: make-data-partition.sh /dev/sdX /dev/sdX2   → prints the new data partition
#
# Only for a freshly flashed Raspberry Pi OS card: two partitions, the root file system
# last, and the rest of the card still empty (Pi OS would grow root into it on the
# first boot). We grow root here instead and put an ext4 partition labelled
# "mlpi-data" at the end of the card for /var/lib/mlpi (all recordings and state).
# MLPI_DATA_SIZE_MB overrides the size (default 4096 on cards ≥ 16 GB, else 2048).

set -euo pipefail

DEV="$1"; ROOT_PART="$2"
die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*" >&2; }

part_name() {   # part_name /dev/sdb 3 → /dev/sdb3; /dev/mmcblk0 3 → /dev/mmcblk0p3
    if [[ "$1" =~ [0-9]$ ]]; then echo "${1}p$2"; else echo "$1$2"; fi
}

ensure_node() {  # a partition the kernel knows but udev hasn't created a node for (yet)
    local name sysdev i
    for i in 1 2 3 4 5; do
        [[ -b "$1" ]] && return 0
        sleep 0.5
    done
    name="${1##*/}"
    sysdev="$(cat "/sys/class/block/$name/dev" 2>/dev/null)" || return 1
    mknod "$1" b "${sysdev%%:*}" "${sysdev##*:}"
}

for tool in sfdisk partx blockdev e2fsck resize2fs mkfs.ext4; do
    command -v "$tool" >/dev/null || die "$tool missing (packages fdisk/util-linux, e2fsprogs)"
done
[[ "$ROOT_PART" == "$(part_name "$DEV" 2)" ]] || die "root isn't partition 2 of $DEV"

read -r nparts p2start p2size < <(sfdisk -d "$DEV" |
    awk -F'[=,]' '/start=/{n++; s=$2; z=$4} END{print n+0, s+0, z+0}')
total="$(blockdev --getsz "$DEV")"
(( nparts == 2 )) || die "expected 2 partitions, found $nparts (only for a freshly flashed card)"
data_mb="${MLPI_DATA_SIZE_MB:-$(( total >= 31000000 ? 4096 : 2048 ))}"
p3start=$(( (total - data_mb * 2048) / 2048 * 2048 ))
# root must keep what it has plus 512 MB of room for the system to grow
(( p3start - p2start >= p2size + 1048576 )) || \
    die "not enough free space after the root partition (card used before? reflash it)"

say "partitioning: root grows to $(( (p3start - p2start) / 2048 )) MB, data partition ${data_mb} MB"
echo "$p2start,$(( p3start - p2start ))" | sfdisk --no-reread --no-tell-kernel -q -N 2 "$DEV"
echo "$p3start,,83" | sfdisk --no-reread --no-tell-kernel -q --append "$DEV"
partx -u "$DEV" 2>/dev/null || partx -a "$DEV" 2>/dev/null || blockdev --rereadpt "$DEV"
udevadm settle 2>/dev/null || true
DATA_PART="$(part_name "$DEV" 3)"
ensure_node "$DATA_PART" || die "$DATA_PART didn't appear"

rc=0
e2fsck -f -y "$ROOT_PART" >/dev/null 2>&1 || rc=$?
(( rc <= 1 )) || die "e2fsck of $ROOT_PART failed ($rc)"
resize2fs "$ROOT_PART" >/dev/null 2>&1 || die "resize2fs of $ROOT_PART failed"
mkfs.ext4 -q -F -L mlpi-data "$DATA_PART"
echo "$DATA_PART"
