#!/usr/bin/env bash
# Turn a freshly flashed Raspberry Pi OS Lite SD card into a MirrorLink-Pi.
# Runs on the LAPTOP (Linux). The Pi itself never needs internet: MirrorLink-Pi is
# pure Python standard library, so this only copies files and writes config.
#
# Usage:
#   sudo ./scripts/prepare-sd.sh /dev/sdX                 # the whole SD card device
#   sudo ./scripts/prepare-sd.sh --boot DIR --root DIR    # partitions already mounted
#   add --phone to also set up phone mode (docs/phone-mode.md): Wi-Fi hotspot,
#   adb + FFmpeg's libavcodec/libswscale installed into the image (needs
#   qemu-user-static on this laptop),
#   the pinned scrcpy server, and the adb key paired with `mlpi pair-phone`.
#   The login is set up here too (default user mlpi, password mlpi, hostname mlpi;
#   --user/--password/--hostname change it; --wifi-password keeps the hotspot password
#   your phone already knows), so Raspberry Pi Imager's own settings
#   are not needed and the first boot never stops at the user-creation wizard.
#   add --data-partition (freshly flashed card only) to put all recordings on their
#   own partition, so a power cut can't damage the system: docs/pi-deployment.md.
#   add --ssh to allow updates over the USB cable later (scripts/update-pi.sh):
#   SSH on, key login with a key made for this laptop (~/.config/mlpi/ssh/),
#   no password login on the USB link and the phone hotspot.
#
# What it does (idempotent, safe to re-run to update the code on the card):
#   rootfs  /opt/mlpi                        code (src, config, systemd, scripts)
#           /etc/systemd/system/mlpi*        units + enable mlpi.target at boot
#           /etc/mlpi/mlpi-self-signed.crt   our self-signed cert served at /cert/
#           /etc/NetworkManager/conf.d/      keep NetworkManager away from usb0
#           /etc/systemd/journald.conf.d/    persistent journal
#           /var/lib/mlpi/                   where each boot's session is recorded
#   bootfs  config.txt                       dtoverlay=dwc2,dr_mode=peripheral
#           mlpi.toml                        user-editable settings (FAT: any OS)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV=""
BOOT=""
ROOT=""
PHONE=0
SSH=0
DATA=0
NEW_USER="mlpi"
NEW_PASSWORD="mlpi"
NEW_HOSTNAME="mlpi"
PASSWORD_GIVEN=0
WIFI_PASSWORD=""
DATA_PART="${MLPI_DATA_PART:-}"     # set by --data-partition (env: tests only)
MOUNTED=()
BINDS=()            # chroot bind mounts: unmounted only, never rmdir'd
RESOLV_SAVED=0

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*"; }

usage() {
    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --boot) BOOT="$2"; shift 2 ;;
        --root) ROOT="$2"; shift 2 ;;
        --phone) PHONE=1; shift ;;
        --ssh) SSH=1; shift ;;
        --data-partition) DATA=1; shift ;;
        --user) NEW_USER="$2"; shift 2 ;;
        --password) NEW_PASSWORD="$2"; PASSWORD_GIVEN=1; shift 2 ;;
        --hostname) NEW_HOSTNAME="$2"; shift 2 ;;
        --wifi-password) WIFI_PASSWORD="$2"; shift 2 ;;
        -h|--help) usage ;;
        /dev/*) DEV="$1"; shift ;;
        *) usage ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
[[ -n "$DEV" || ( -n "$BOOT" && -n "$ROOT" ) ]] || usage

cleanup() {
    local m
    [[ -n "$ROOT" ]] && { cleanup_chroot 2>/dev/null || true; }
    for m in "${MOUNTED[@]:-}"; do
        [[ -n "$m" ]] || continue
        umount "$m" 2>/dev/null || true
        rmdir "$m" 2>/dev/null || true
    done
}
trap cleanup EXIT

data_partition_config() {
    # Mount it at /var/lib/mlpi. nofail: if it's ever damaged the Pi still boots
    # (and records onto the root file system). fsck repairs it at boot (pass 2).
    sed -i '/[[:space:]]\/var\/lib\/mlpi[[:space:]]/d' "$ROOT/etc/fstab"
    echo "LABEL=mlpi-data  /var/lib/mlpi  ext4  defaults,noatime,commit=5,nofail,x-systemd.device-timeout=10s  0  2" \
        >> "$ROOT/etc/fstab"
    # The system journal stays in RAM: nothing on the root file system is written
    # during a drive (mlpi copies the journal into each session anyway).
    cat > "$ROOT/etc/systemd/journald.conf.d/mlpi.conf" <<'CONF'
# MirrorLink-Pi with a data partition: journal in RAM, the root file system stays idle.
[Journal]
Storage=volatile
RuntimeMaxUse=32M
CONF
    rm -rf "$ROOT/var/log/journal"
    # Root already fills its partition. Pi OS's first-boot resize (Trixie: " resize" →
    # parted resizepart 2 to the end of the card in the initramfs; Bookworm: the
    # firstboot init) would now collide with the data partition, and parted can stop
    # and wait for an answer on the console. So it must not run.
    sed -i -e 's# init=/usr/lib/raspberrypi-sys-mods/firstboot##' -e 's# resize\b##g' \
        "$BOOT/cmdline.txt"
    # Settings and keys written to the root fs's /var/lib/mlpi (e.g. the adb key) go
    # onto the data partition, which hides that directory once mounted.
    local mnt
    mnt="$(mktemp -d /tmp/mlpi-data.XXXX)"
    mount "$DATA_PART" "$mnt"
    tar -C "$ROOT/var/lib/mlpi" --exclude=./sessions -cf - . | tar -C "$mnt" -xpf -
    umount "$mnt"
    rmdir "$mnt"
    say "data partition $DATA_PART → /var/lib/mlpi"
}

if [[ -n "$DEV" ]]; then
    [[ -b "$DEV" ]] || die "$DEV is not a block device"
    ROOT_SRC="$(findmnt -no SOURCE / || true)"
    if [[ -n "$ROOT_SRC" ]] && lsblk -lnpo NAME "$DEV" | grep -qx "$ROOT_SRC"; then
        die "$DEV holds this laptop's root filesystem — wrong device!"
    fi
    # Right after writing an image, udev may still be identifying the partitions.
    for _ in $(seq 20); do
        BOOT_PART="$(lsblk -lnpo NAME,FSTYPE "$DEV" | awk '$2=="vfat"{print $1; exit}')"
        ROOT_PART="$(lsblk -lnpo NAME,FSTYPE "$DEV" | awk '$2=="ext4"{print $1; exit}')"
        [[ -n "$BOOT_PART" && -n "$ROOT_PART" ]] && break
        sleep 0.5
    done
    [[ -n "$BOOT_PART" && -n "$ROOT_PART" ]] || \
        die "$DEV does not look like a Raspberry Pi OS card (need a vfat and an ext4 partition)"
    # Desktop environments auto-mount the card; take the partitions over.
    for part in "$BOOT_PART" "$ROOT_PART"; do
        while read -r mp; do
            [[ -n "$mp" ]] && umount "$mp"
        done < <(lsblk -lno MOUNTPOINT "$part")
    done
    DATA_PART="$(lsblk -lnpo NAME,LABEL "$DEV" | awk '$2=="mlpi-data"{print $1; exit}')"
    [[ -n "$DATA_PART" ]] && say "data partition: $DATA_PART"
    if (( DATA )) && [[ -z "$DATA_PART" ]]; then
        DATA_PART="$("$REPO/scripts/make-data-partition.sh" "$DEV" "$ROOT_PART")" || \
            die "couldn't create the data partition (card unchanged unless said otherwise above)"
    fi
    BOOT="$(mktemp -d /tmp/mlpi-boot.XXXX)"; mount "$BOOT_PART" "$BOOT"; MOUNTED+=("$BOOT")
    ROOT="$(mktemp -d /tmp/mlpi-root.XXXX)"; mount "$ROOT_PART" "$ROOT"; MOUNTED+=("$ROOT")
    say "mounted $BOOT_PART → $BOOT, $ROOT_PART → $ROOT"
fi

# ---------- sanity checks ----------

[[ -f "$BOOT/config.txt" ]] || die "$BOOT/config.txt missing — is --boot the FAT boot partition?"
[[ -f "$ROOT/etc/os-release" ]] || die "$ROOT/etc/os-release missing — is --root the rootfs?"
. <(grep -E '^(PRETTY_NAME|VERSION_CODENAME)=' "$ROOT/etc/os-release")
say "image: ${PRETTY_NAME:-unknown}"

PY="$(ls "$ROOT"/usr/bin/python3.[0-9]* 2>/dev/null | grep -E 'python3\.[0-9]+$' | sort -V | tail -1 || true)"
[[ -n "$PY" ]] || die "no python3 in the image — use Raspberry Pi OS Lite (it ships python3)"
PYVER="${PY##*/python3.}"
(( PYVER >= 11 )) || die "image has Python 3.$PYVER; MirrorLink-Pi needs 3.11+ (Bookworm or newer)"
say "python 3.$PYVER in image: OK"

if grep -q 'g_ether\|g_multi' "$BOOT/cmdline.txt" 2>/dev/null; then
    echo "WARNING: cmdline.txt loads a legacy USB gadget module (g_ether/g_multi)." >&2
    echo "         mlpi unloads it at boot, but better remove it from $BOOT/cmdline.txt" >&2
fi

# ---------- code ----------

say "installing code into /opt/mlpi"
rm -rf "$ROOT/opt/mlpi"
install -d -m 0755 "$ROOT/opt/mlpi"
tar -C "$REPO" --exclude='__pycache__' --exclude='*.pyc' -cf - \
    src config systemd scripts docs README.md LICENSE pyproject.toml \
    | tar -C "$ROOT/opt/mlpi" --no-same-owner -xf -
{
    echo "prepared: $(date -u +%FT%TZ)"
    git -C "$REPO" describe --always --dirty --tags 2>/dev/null | sed 's/^/git: /' || true
} > "$ROOT/opt/mlpi/VERSION"

# ---------- systemd ----------

say "installing systemd units"
SYSD="$ROOT/etc/systemd/system"
install -d "$SYSD"
for old in dnsmasq-usb0.service mlpi-upnp.service mlpi-xvfb.service mlpi-vnc.service; do
    rm -f "$SYSD/$old" "$SYSD"/*.wants/"$old"
done
install -m 0644 "$REPO"/systemd/*.service "$REPO"/systemd/mlpi.target "$SYSD/"
install -d "$SYSD/multi-user.target.wants"
ln -sfn /etc/systemd/system/mlpi.target "$SYSD/multi-user.target.wants/mlpi.target"

# Pi OS (Trixie) ships rpi-usb-gadget: when turned on (e.g. Imager's "USB gadget mode")
# it loads g_ether at boot and lets NetworkManager share its connection over usb0.
# Both would fight our gadget, so mask its service and drop its module list.
ln -sfn /dev/null "$SYSD/rpi-usb-gadget-ics.service"
rm -f "$SYSD/multi-user.target.wants/rpi-usb-gadget-ics.service"
if [[ -f "$ROOT/etc/modules-load.d/usb-gadget.conf" ]]; then
    say "removing rpi-usb-gadget's g_ether autoload"
    rm -f "$ROOT/etc/modules-load.d/usb-gadget.conf"
fi

# ---------- system config ----------

install -d -m 0755 "$ROOT/etc/mlpi" "$ROOT/var/lib/mlpi"
rm -f "$ROOT/etc/mlpi/self-signed.ccc.crt"   # third-party cert from older versions
install -m 0644 "$REPO/config/mlpi-self-signed.crt" "$ROOT/etc/mlpi/" 2>/dev/null \
    || echo "note: config/mlpi-self-signed.crt not found in repo; /cert/ will return 404" >&2

install -d "$ROOT/etc/NetworkManager/conf.d"
cat > "$ROOT/etc/NetworkManager/conf.d/99-mlpi-usb0.conf" <<'EOF'
# MirrorLink-Pi configures usb0 itself (static 192.168.7.2 + own DHCP server).
[keyfile]
unmanaged-devices=interface-name:usb0
EOF

install -d "$ROOT/etc/systemd/journald.conf.d" "$ROOT/var/log/journal"
cat > "$ROOT/etc/systemd/journald.conf.d/mlpi.conf" <<'EOF'
# MirrorLink-Pi: keep logs across reboots (the car cuts power without warning).
[Journal]
Storage=persistent
SyncIntervalSec=5s
SystemMaxUse=200M
EOF

# ---------- boot partition ----------

# Enable USB device (gadget) mode. We look ONLY for our exact peripheral overlay:
# stock Pi OS ships model-specific dwc2 lines (e.g. "[cm5] dtoverlay=dwc2,dr_mode=host")
# that a Pi Zero 2 W ignores, so a loose "any dwc2?" check is wrong. We always append
# our line under a fresh [all] section, which every model reads, so it applies
# regardless of the model-specific sections above it.
if grep -qE '^[[:space:]]*dtoverlay=dwc2,dr_mode=peripheral' "$BOOT/config.txt"; then
    say "USB device mode (dwc2 peripheral) already set in config.txt"
else
    say "enabling USB device mode (dwc2 peripheral) in config.txt"
    printf '\n# MirrorLink-Pi: USB device (gadget) mode on the Pi Zero USB port\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' \
        >> "$BOOT/config.txt"
fi

if [[ ! -f "$BOOT/mlpi.toml" ]]; then
    install -m 0644 "$REPO/config/mlpi.toml.example" "$BOOT/mlpi.toml"
    say "wrote $BOOT/mlpi.toml (edit it from any computer to change settings)"
fi

# ---------- first-boot user check ----------

if ! grep -qE '^[^:]+:[^:]*:1000:' "$ROOT/etc/passwd" && [[ ! -f "$BOOT/userconf.txt" ]] \
        && [[ ! -f "$BOOT/user-data" ]] && [[ ! -f "$BOOT/firstrun.sh" ]]; then
    echo "WARNING: no user account configured. MirrorLink-Pi runs anyway, but you will not" >&2
    echo "         be able to log in. Set user/password in Raspberry Pi Imager next time." >&2
fi

# ---------- phone mode (optional) ----------

phone_packages() {
    # adb + FFmpeg's decoder libraries from the image's own apt sources, installed by
    # running apt inside the image with qemu. apt's package lists and download cache
    # live in a temp dir on this laptop: a freshly written card has only ~300 MB free
    # until its first boot, and the packages themselves need ~100 MB.
    # (python3-av would need ~410 MB and does not fit — see src/mlpi/avdecode.py.)
    local machine arch aptdir codec sws O
    if [[ -x "$ROOT/usr/bin/adb" ]] && compgen -G "$ROOT/usr/lib/*/libavcodec.so.*" >/dev/null \
            && compgen -G "$ROOT/usr/lib/*/libswscale.so.*" >/dev/null \
            && [[ -x "$ROOT/usr/sbin/iw" ]]; then
        say "adb and libavcodec already in the image"
        return
    fi
    machine="$(od -An -t u1 -j 18 -N 1 "$ROOT/usr/bin/dpkg" | tr -d ' ')"
    case "$machine" in
        183) arch=aarch64 ;;
        40)  arch=arm ;;
        *)   die "cannot tell the image's CPU architecture (ELF machine $machine)" ;;
    esac
    [[ -e /proc/sys/fs/binfmt_misc/qemu-$arch ]] || \
        die "installing packages into the image needs qemu: sudo apt install qemu-user-static"
    say "installing adb + libavcodec into the image (qemu $arch, takes a few minutes)"
    local d
    for d in dev dev/pts proc sys; do
        mount --bind "/$d" "$ROOT/$d"
        BINDS+=("$ROOT/$d")
    done
    aptdir="$(mktemp -d /tmp/mlpi-apt.XXXX)"
    mkdir -p "$aptdir/lists/partial" "$aptdir/cache/archives/partial" "$ROOT/mlpi-apt"
    mount --bind "$aptdir" "$ROOT/mlpi-apt"
    BINDS+=("$ROOT/mlpi-apt")
    if [[ -e "$ROOT/etc/resolv.conf" || -L "$ROOT/etc/resolv.conf" ]]; then
        mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.mlpi-saved"
        RESOLV_SAVED=1
    fi
    cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
    printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"   # start no services
    chmod 0755 "$ROOT/usr/sbin/policy-rc.d"
    O=(-o Dir::State::Lists=/mlpi-apt/lists -o Dir::Cache=/mlpi-apt/cache -o APT::Sandbox::User=root)
    chroot "$ROOT" apt-get "${O[@]}" update -qq
    # The library package names carry the FFmpeg ABI version (e.g. libavcodec61).
    codec="$(chroot "$ROOT" apt-cache "${O[@]}" pkgnames libavcodec | grep -E '^libavcodec[0-9]+$' | sort -V | tail -1)"
    sws="$(chroot "$ROOT" apt-cache "${O[@]}" pkgnames libswscale | grep -E '^libswscale[0-9]+$' | sort -V | tail -1)"
    [[ -n "$codec" && -n "$sws" ]] || die "no libavcodec/libswscale package found in the image's apt sources"
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
        apt-get "${O[@]}" install -y -qq --no-install-recommends adb "$codec" "$sws" iw
    cleanup_chroot
    rm -rf "$aptdir"
    rmdir "$ROOT/mlpi-apt" 2>/dev/null || true
}

cleanup_chroot() {
    local i
    rm -f "$ROOT/usr/sbin/policy-rc.d"
    if (( RESOLV_SAVED )); then
        rm -f "$ROOT/etc/resolv.conf"
        mv "$ROOT/etc/resolv.conf.mlpi-saved" "$ROOT/etc/resolv.conf"
        RESOLV_SAVED=0
    fi
    for (( i=${#BINDS[@]}-1; i>=0; i-- )); do
        umount "${BINDS[i]}" || umount -l "${BINDS[i]}"
    done
    BINDS=()
}

phone_setup() {
    local user_home keydir password
    say "phone mode"
    # 1. scrcpy server (version + checksum pinned in the fetch script)
    if [[ -n "${MLPI_SCRCPY_SERVER:-}" ]]; then
        install -D -m 0644 "$MLPI_SCRCPY_SERVER" "$ROOT/opt/mlpi/vendor/scrcpy-server"
    else
        "$REPO/scripts/fetch-scrcpy-server.sh" "$ROOT/opt/mlpi/vendor/scrcpy-server"
    fi
    # 2. packages the Pi cannot download itself
    if [[ -z "${MLPI_PHONE_SKIP_PACKAGES:-}" ]]; then
        phone_packages
    fi
    # 3. the adb key the phone trusts (mlpi pair-phone), never generated on the Pi
    user_home="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)"
    keydir="${MLPI_ADB_KEYDIR:-$user_home/.config/mlpi/adb/.android}"
    if [[ ! -f "$keydir/adbkey" ]] && command -v adb >/dev/null; then
        install -d -m 0700 "$keydir"
        adb keygen "$keydir/adbkey" >/dev/null 2>&1 || true
        [[ -n "${SUDO_USER:-}" ]] && chown -R "$SUDO_USER:" "$(dirname "$keydir")" || true
    fi
    if [[ -f "$keydir/adbkey" ]]; then
        install -d -m 0700 "$ROOT/var/lib/mlpi/adb/.android"
        install -m 0600 "$keydir/adbkey" "$ROOT/var/lib/mlpi/adb/.android/adbkey"
        [[ -f "$keydir/adbkey.pub" ]] && \
            install -m 0644 "$keydir/adbkey.pub" "$ROOT/var/lib/mlpi/adb/.android/adbkey.pub"
        say "adb key copied from $keydir"
    else
        echo "WARNING: no adb key found at $keydir. Install adb on this laptop" >&2
        echo "         (sudo apt install adb), run 'mlpi pair-phone', then re-run with --phone." >&2
    fi
    # 4. settings on the boot partition: enable phone mode with a random Wi-Fi password
    if ! grep -q '^\[phone\]' "$BOOT/mlpi.toml"; then
        password="${WIFI_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')}"
        (( ${#password} >= 8 && ${#password} <= 63 )) || die "--wifi-password: 8 to 63 characters"
        printf '\n[phone]\nenabled = true\nwifi_ssid = "MirrorLink-Pi"\nwifi_password = "%s"\nwifi_country = "DE"\n' \
            "$password" >> "$BOOT/mlpi.toml"
    fi
    PHONE_SSID="$(sed -n '/^\[phone\]/,/^\[/s/^wifi_ssid *= *"\(.*\)"/\1/p' "$BOOT/mlpi.toml" | head -1)"
    PHONE_PSK="$(sed -n '/^\[phone\]/,/^\[/s/^wifi_password *= *"\(.*\)"/\1/p' "$BOOT/mlpi.toml" | head -1)"
}

PHONE_SSID=""
PHONE_PSK=""
if (( PHONE )); then
    phone_setup
fi

# ---------- SSH for updates over the USB cable (--ssh) ----------

ssh_setup() {
    # The Pi user is only created at first boot (from Imager's settings), so the key
    # can't go into its home yet: /etc/mlpi/authorized_keys works for any user.
    local owner home keydir unit
    owner="${SUDO_USER:-root}"
    home="$(getent passwd "$owner" | cut -d: -f6)"
    keydir="${MLPI_SSH_KEYDIR:-$home/.config/mlpi/ssh}"
    if [[ ! -f "$keydir/id_ed25519" ]]; then
        command -v ssh-keygen >/dev/null || die "--ssh needs ssh-keygen (package openssh-client)"
        sudo -u "$owner" mkdir -p "$keydir"
        chmod 0700 "$keydir"
        sudo -u "$owner" ssh-keygen -q -t ed25519 -N "" -C "mlpi-update" -f "$keydir/id_ed25519"
        say "created the update key $keydir/id_ed25519"
    fi
    install -d -m 0755 "$ROOT/etc/mlpi" "$ROOT/etc/ssh/sshd_config.d"
    install -m 0644 "$keydir/id_ed25519.pub" "$ROOT/etc/mlpi/authorized_keys"
    cat > "$ROOT/etc/ssh/sshd_config.d/mlpi.conf" <<'CONF'
# MirrorLink-Pi (prepare-sd.sh --ssh): the laptop that prepared the card may log in
# with its update key; on the USB link and the phone hotspot only with keys.
AuthorizedKeysFile .ssh/authorized_keys /etc/mlpi/authorized_keys
Match LocalAddress 192.168.7.2,192.168.8.1
    PasswordAuthentication no
    KbdInteractiveAuthentication no
CONF
    unit=""
    for candidate in "$ROOT"/usr/lib/systemd/system/ssh.service \
                     "$ROOT"/lib/systemd/system/ssh.service; do
        [[ -f "$candidate" ]] && { unit="$candidate"; break; }
    done
    install -d "$ROOT/etc/systemd/system/multi-user.target.wants"
    if [[ -n "$unit" ]]; then
        ln -sfn "/${unit#"$ROOT"/}" "$ROOT/etc/systemd/system/multi-user.target.wants/ssh.service"
    else
        touch "$BOOT/ssh"         # older images: enabled at first boot by sshswitch
    fi
    say "SSH enabled: update later with ./scripts/update-pi.sh <pi-user>@192.168.7.2"
}

# ---------- login: user, password, hostname (no Imager settings needed) ----------

account_setup() {
    # Pi OS images ship a placeholder user "pi" (uid 1000, no shell, locked). On first
    # boot userconfig.service renames it from userconf.txt, Imager's cloud-init
    # settings, or (if neither took) an interactive wizard on the console: the boot
    # then waits forever on a headless Pi. We do the rename here, offline, and switch
    # those first-boot steps off.
    local first hash
    first="$(awk -F: '$3==1000{print $1}' "$ROOT/etc/passwd")"
    if [[ "$first" == "$NEW_USER" ]]; then
        if (( PASSWORD_GIVEN )); then
            hash="$(openssl passwd -6 "$NEW_PASSWORD")"
            python3 - "$ROOT" "$NEW_USER" "$hash" <<'PY'
import sys, pathlib
root, user, hash_ = sys.argv[1:]
p = pathlib.Path(root, "etc/shadow")
p.write_text("".join(
    ":".join([user, hash_] + l.split(":")[2:]) if l.split(":")[0] == user else l
    for l in p.read_text().splitlines(True)))
PY
            say "password of $NEW_USER changed"
        else
            say "user $NEW_USER already set up"
        fi
        return
    fi
    if [[ "$first" != "pi" ]] || ! grep -q '^pi:[^:]*:1000:.*nologin' "$ROOT/etc/passwd"; then
        say "user '${first:-?}' already set up on this card (not touching it)"
        return
    fi
    [[ "$NEW_USER" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || die "--user: lower-case letters, digits, '-'"
    command -v openssl >/dev/null || die "setting the password needs openssl on this laptop"
    hash="$(openssl passwd -6 "$NEW_PASSWORD")"
    python3 - "$ROOT" "$NEW_USER" "$hash" <<'PY'
import pathlib, sys
root, new, hash_ = sys.argv[1:]
etc = pathlib.Path(root, "etc")

def rewrite(name, fix):
    p = etc / name
    if p.exists():
        p.write_text("".join(fix(l.rstrip("\n").split(":")) + "\n"
                             for l in p.read_text().splitlines()))

def members(field):                       # "a,pi,b" → "a,<new>,b"
    return ",".join(new if m == "pi" else m for m in field.split(",")) if field else field

def passwd(f):
    if f[0] == "pi":
        f[0], f[5], f[6] = new, f"/home/{new}", "/bin/bash"
    return ":".join(f)

def shadow(f):
    if f[0] == "pi":
        f[0], f[1] = new, hash_
    return ":".join(f)

def group(f):
    f[0] = new if f[0] == "pi" else f[0]
    f[3] = members(f[3])
    return ":".join(f)

def gshadow(f):
    f[0] = new if f[0] == "pi" else f[0]
    f[2], f[3] = members(f[2]), members(f[3])
    return ":".join(f)

def subid(f):
    f[0] = new if f[0] == "pi" else f[0]
    return ":".join(f)

rewrite("passwd", passwd)
rewrite("shadow", shadow)
rewrite("group", group)
rewrite("gshadow", gshadow)
rewrite("subuid", subid)
rewrite("subgid", subid)
PY
    if [[ -d "$ROOT/home/pi" && ! -e "$ROOT/home/$NEW_USER" ]]; then
        mv "$ROOT/home/pi" "$ROOT/home/$NEW_USER"
    fi
    install -d -m 0755 "$ROOT/etc/sudoers.d"
    echo "$NEW_USER ALL=(ALL) NOPASSWD: ALL" > "$ROOT/etc/sudoers.d/010_mlpi-nopasswd"
    chmod 0440 "$ROOT/etc/sudoers.d/010_mlpi-nopasswd"
    # What cancel-rename does once the user exists: wizard off, console login on.
    rm -f "$ROOT/etc/systemd/system/multi-user.target.wants/userconfig.service"
    install -d "$ROOT/etc/systemd/system/getty.target.wants"
    ln -sfn /usr/lib/systemd/system/getty@.service \
        "$ROOT/etc/systemd/system/getty.target.wants/getty@tty1.service"
    rm -f "$ROOT/etc/ssh/sshd_config.d/rename_user.conf"
    # cloud-init only applies Imager's settings; with the work done it must not
    # re-create "pi" or rename the host.
    if [[ -d "$ROOT/etc/cloud" ]]; then
        touch "$ROOT/etc/cloud/cloud-init.disabled"
    fi
    rm -f "$BOOT/userconf" "$BOOT/userconf.txt" "$BOOT/firstrun.sh"
    if (( PASSWORD_GIVEN )); then
        say "login: user $NEW_USER, password as given"
    else
        say "login: user $NEW_USER, password $NEW_PASSWORD (change it with --password)"
    fi
}

hostname_setup() {
    local old
    old="$(cat "$ROOT/etc/hostname" 2>/dev/null || echo raspberrypi)"
    [[ "$old" == "$NEW_HOSTNAME" ]] && return
    echo "$NEW_HOSTNAME" > "$ROOT/etc/hostname"
    if grep -q '^127\.0\.1\.1' "$ROOT/etc/hosts" 2>/dev/null; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t\t$NEW_HOSTNAME/" "$ROOT/etc/hosts"
    else
        printf '127.0.1.1\t\t%s\n' "$NEW_HOSTNAME" >> "$ROOT/etc/hosts"
    fi
    say "hostname: $NEW_HOSTNAME"
}

account_setup
hostname_setup

if (( SSH )); then
    ssh_setup
fi

if [[ -n "$DATA_PART" ]]; then
    data_partition_config
elif (( DATA )); then
    echo "WARNING: --data-partition needs the card device (/dev/sdX), not --boot/--root" >&2
fi

sync
say "done. Put the card in the Pi. First boot takes ~1-2 min (the image resizes itself)."
echo
echo "Pre-flight at home (recommended, see docs/field-test.md): plug the Pi's USB (not PWR)"
echo "port into this laptop, wait until the LED blinks 2× (laptop got an address), then:"
echo "    PYTHONPATH=src python3 -m mlpi simulate-car --target 192.168.7.2"
if (( PHONE )); then
    echo
    echo "Phone mode: join the phone to Wi-Fi '${PHONE_SSID:-MirrorLink-Pi}' (password: ${PHONE_PSK:-see mlpi.toml})"
    echo "and switch on Wireless debugging. Details: docs/phone-mode.md"
fi
