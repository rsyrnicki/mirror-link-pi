#!/bin/sh
# Runs ON THE PI, ~2 minutes after power-on (mlpi-bootcheck.service). If MirrorLink-Pi
# isn't running by then, save why to the boot partition: the system journal is kept
# in RAM (power-cut protection), so without this a failed start leaves no trace.
# The file is plain text on the FAT partition, readable on any computer:
#   bootfs/mlpi-boot-problem.txt     (collect-logs.sh copies it too)
# Usage: boot-check.sh [OUT]          (OUT defaults to /boot/firmware/mlpi-boot-problem.txt)

OUT="${1:-/boot/firmware/mlpi-boot-problem.txt}"

problem=""
systemctl is-active --quiet mlpi.service || problem="$problem mlpi.service-not-running"
[ -s /run/mlpi/session-dir ] || problem="$problem no-session"
jobs="$(systemctl list-jobs --no-legend 2>/dev/null | head -20)"
[ -n "$jobs" ] && problem="$problem boot-still-waiting"
[ -z "$problem" ] && exit 0

{
    echo "MirrorLink-Pi boot problem:$problem"
    echo "uptime: $(cut -d' ' -f1 /proc/uptime) s; throttled: $(vcgencmd get_throttled 2>/dev/null)"
    echo
    echo "== systemd jobs still waiting (a stuck one blocks everything after it)"
    echo "${jobs:-none}"
    echo
    echo "== failed units"
    systemctl --failed --no-legend --no-pager 2>&1
    echo
    echo "== mlpi units"
    systemctl status --no-pager --lines=15 'mlpi*' 2>&1 | head -150
    echo
    echo "== /var/lib/mlpi"
    findmnt /var/lib/mlpi 2>&1
    df -h / /var/lib/mlpi 2>&1
    echo
    echo "== kernel: SD card, file systems, power"
    dmesg 2>/dev/null | grep -iE 'mmc|ext4|voltage|i/o error|read-only' | tail -40
    echo
    echo "== journal (last 300 lines)"
    journalctl -b --no-pager -o short-monotonic 2>&1 | tail -300
} > "$OUT.tmp" 2>&1 && mv -f "$OUT.tmp" "$OUT" && sync
