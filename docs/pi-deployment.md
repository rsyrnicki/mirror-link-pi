# Preparing the SD card (on the laptop, offline for the Pi)

Target: **Raspberry Pi Zero 2 W** with **Raspberry Pi OS Lite (64-bit)** — the current
release (Debian 13 "Trixie", Python 3.13). Bookworm (Python 3.11) works too.

The whole installation happens on the laptop. The Pi never needs internet:
MirrorLink-Pi uses only the Python standard library that ships with Pi OS Lite. It
brings its own DHCP server, VNC server and packet recorder (no dnsmasq, x11vnc, Xvfb or
tcpdump).

**Phone mode** (`--phone`, see [`phone-mode.md`](phone-mode.md)) additionally installs
`adb`, `iw` and FFmpeg's H.264 decoder libraries into the image. The script does that
from the laptop by running the image's own `apt` under QEMU, so the **laptop needs
internet** for this step and the `qemu-user-static` package.

Laptop requirements: Linux (tested on Fedora and Ubuntu), `sudo`, Python ≥ 3.11,
`openssl`, the repo checked out, and for phone mode `adb` + `qemu-user-static`
(Debian/Ubuntu: `sudo apt install adb qemu-user-static openssl`;
Fedora: `sudo dnf install android-tools qemu-user-static openssl`).

## 1. The easy way: one command

Pair the phone first ([`phone-mode.md`](phone-mode.md#pair-the-phone-with-the-pis-key)),
then:

```bash
lsblk                                   # find the card, e.g. /dev/sdb or /dev/mmcblk0
sudo ./scripts/install-sd.sh /dev/sdX   # the whole device; it asks before erasing
```

It downloads the latest Raspberry Pi OS Lite (64-bit) from raspberrypi.com (cached in
`~/.cache/mlpi/images`, checksum-verified), writes it to the card and runs
`prepare-sd.sh --phone --ssh --data-partition` on it. That's everything: phone mode,
updates over USB, power-cut protection. Options: `--no-phone`, `--image FILE` (a
downloaded `.img`/`.img.xz`), `--user`/`--password`/`--hostname`, and
`--wifi-password PW` to keep the hotspot password a phone already has saved
(otherwise a new random one is set and printed at the end).

**Login on the Pi:** user `mlpi`, password `mlpi`, hostname `mlpi`. The script sets
these up itself, offline: no Raspberry Pi Imager settings are involved, so the first
boot can't stop at the "create a user" screen. Use `--password` for your own.

## 2. Or step by step: Raspberry Pi Imager + prepare-sd.sh

1. In Imager: Device *Raspberry Pi Zero 2 W*, OS *Raspberry Pi OS (other) → Raspberry
   Pi OS Lite (64-bit)*. When it asks about **OS customisation, choose "No"**: settings
   made there don't always take on current images, and prepare-sd.sh sets the login.
2. Write. Then take the card out and put it back in (so both partitions show up).
3. Install MirrorLink-Pi onto it:

```bash
lsblk                                   # find the card, e.g. /dev/sdb or /dev/mmcblk0
sudo ./scripts/prepare-sd.sh /dev/sdX   # the whole device, not a partition
# or, with phone mode (pair the phone first: see phone-mode.md):
sudo ./scripts/prepare-sd.sh --phone /dev/sdX
```

Double-check the device name with `lsblk` (size, removable): the script writes to it.
The login becomes `mlpi`/`mlpi` with hostname `mlpi` (`--user`, `--password`,
`--hostname` to change); a card that already has a user keeps it.

Recommended on a **freshly flashed** card, before its first boot: add
`--data-partition` (see [Protecting the card against power cuts](#protecting-the-card-against-power-cuts)).

If your desktop mounted the partitions already, the script unmounts and remounts
them itself. Alternatively: `sudo ./scripts/prepare-sd.sh --boot /run/media/$USER/bootfs --root /run/media/$USER/rootfs`.

What it writes:

| Where | What |
|---|---|
| rootfs `/opt/mlpi/` | the code (src, config, systemd, scripts, docs) + `VERSION` |
| rootfs `/etc/systemd/system/` | `mlpi.target` + 5 units, enabled at boot |
| rootfs `/etc/mlpi/mlpi-self-signed.crt` | our self-signed test cert served on `/cert/` |
| rootfs `/etc/NetworkManager/conf.d/99-mlpi-usb0.conf` | NetworkManager leaves `usb0` alone |
| rootfs `/etc/systemd/journald.conf.d/mlpi.conf` | persistent journal (in RAM with `--data-partition`) |
| rootfs `/etc/passwd`, `shadow`, `group`, … | the placeholder user `pi` becomes `mlpi` with its password; the first-boot wizard and cloud-init are switched off |
| rootfs `/etc/hostname`, `/etc/hosts` | `mlpi` |
| rootfs `rpi-usb-gadget-ics.service` → masked | Pi OS's own USB-gadget helper can't grab `usb0` |
| bootfs `config.txt` | `dtoverlay=dwc2,dr_mode=peripheral` (USB device mode) |
| bootfs `mlpi.toml` | settings you can edit from any OS (see below) |

Re-running the script updates the code on the card and keeps recorded sessions.
Add `--ssh` once to be able to update **without taking the card out** later (see
[Updating over the USB cable](#updating-over-the-usb-cable)).

## 3. First boot

Insert the card, power the Pi. The first boot of a fresh image resizes the file system
and reboots once; allow 1–2 minutes. From then on, every boot starts:

| Unit | Does |
|---|---|
| `mlpi-session.service` | creates `/var/lib/mlpi/sessions/NNNN` for this boot |
| `mlpi-gadget.service` | USB gadget (CDC-NCM) + `usb0` = 192.168.7.2/24; stays running, logs USB events to `usb.jsonl`. Answering the MirrorLink USB command is opt-in (`[usb] ml_command`, off by default) |
| `mlpi-capture.service` | records every frame on `usb0` into the session pcap |
| `mlpi-journal.service` | copies the whole boot journal into the session |
| `mlpi.service` | DHCP, SSDP, UPnP/SOAP, VNC server, status screen, LED |

**Which USB port:** the Pi Zero 2 W has two micro-USB ports. The one marked **USB**
(closer to the middle) is the data port — the car/laptop cable goes there. It also
powers the Pi, so the PWR port stays empty.

## 4. Pre-flight check at home (5 minutes, strongly recommended)

See [`field-test.md`](field-test.md#pre-flight-at-home). In short: plug the Pi into the
laptop's USB port, wait for the LED to blink twice, run
`PYTHONPATH=src python3 -m mlpi simulate-car --target 192.168.7.2`, and look at the
saved `car-view.png` — that is the picture the car should get.

## 5. Settings (`mlpi.toml` on the boot partition)

The boot partition is FAT, so it can be edited on any computer. Useful knobs:

- `[experiment] mode = "fixed"` + `fixed_variant = "s6-audio-home"` — stop rotating and
  always use one variant. `s6-audio-home` is the one the VW MIB2 accepts; with the default
  `rotate` the Pi starts with it anyway and only moves on if the car doesn't connect.
- `[phone] wifi_country` — your country code (default `DE`); it sets the legal Wi-Fi
  channels and power for the Pi's hotspot.
- `[usb] vid/pid` — pretend to be another vendor if a head unit filters on it.
- `[watchdog] idle_reconnect_seconds = 0` — never soft re-plug.
- `[usb] ml_command = true` — also answer the MirrorLink USB command via a FunctionFS
  interface (experimental; off by default, needs a reboot to take effect).

Variants themselves are in `/opt/mlpi/config/variants.toml` on the rootfs (re-run
`prepare-sd.sh` after editing the repo copy).

## 6. Getting the logs back

Without taking the card out (card prepared with `--ssh`, as `install-sd.sh` does): plug
the Pi's USB port into the laptop, wait for 2 LED blinks, then

```bash
./scripts/collect-logs.sh --pi                   # → ./car-logs/<timestamp>/REPORT.txt
```

With the card in the laptop:

```bash
sudo ./scripts/collect-logs.sh /dev/sdX          # → ./car-logs/<timestamp>/REPORT.txt
```

Both also collect two things that only show up when something went wrong:

- **`mlpi-boot-problem.txt`:** if MirrorLink-Pi isn't running 2 minutes after
  power-on (the car then sees no MirrorLink device, the LED stays solid), the Pi
  writes the reason to this file on the boot partition (`bootfs`). It's plain text,
  readable on any computer. The system journal is kept in RAM, so this file is the
  only trace such a start leaves.
- **`zips/rootfs-session-NNNN.zip`:** sessions recorded while the data partition
  wasn't mounted (they land on the root file system instead).

If the Pi isn't reachable over USB, use the card in the laptop. The same goes for
updates: `sudo ./scripts/update-pi.sh --card /dev/sdX` installs the current code
straight onto the card (keeps recordings, settings and the scrcpy server).

## Protecting the card against power cuts

The car cuts the Pi's power without warning. Anything being written at that moment
can be damaged, and on a normal card that includes the system itself. With
`--data-partition` (freshly flashed card, before the first boot):

- the end of the card becomes a separate partition `mlpi-data` (4 GB on cards of 16 GB
  or more, else 2 GB) mounted at `/var/lib/mlpi`: every recording, the adb key, the
  remembered phone details;
- the system journal stays in RAM, so during a drive **the system partition isn't
  written at all**;
- the data partition is checked and repaired at boot, and if it's ever unusable the
  Pi still starts (it then records onto the system partition).

The root partition is grown to fill the rest of the card right away, so Pi OS's own
first-boot resize is switched off. The script refuses to repartition a card that has
been booted or used before; reflash it first. `collect-logs.sh` finds the recordings on
either layout.

## Updating over the USB cable

Once a card has been prepared with `--ssh`, new versions go onto the Pi while it's
plugged into the laptop, no card swapping:

```bash
git pull
./scripts/update-pi.sh             # = mlpi@192.168.7.2; other login: ./scripts/update-pi.sh USER@192.168.7.2
```

It copies the code over SSH, keeps the scrcpy server and every recording, and reboots
the Pi (about 30 s). What `--ssh` set up:

- SSH is switched on. Logging in on the USB link (192.168.7.2) and the phone hotspot
  (192.168.8.1) works **only with keys**, no passwords.
- A key made for this laptop, `~/.config/mlpi/ssh/id_ed25519`, is allowed for any user
  on the Pi (`/etc/mlpi/authorized_keys`). Keep it private like any SSH key.

New system packages can't be installed this way, since the Pi has no internet. If an
update needs one, the update says so; then run `sudo ./scripts/prepare-sd.sh --phone
/dev/sdX` once with the card in the laptop. If the card was re-flashed, delete
`~/.config/mlpi/ssh/known_hosts` (the Pi has new host keys).

## Troubleshooting on the Pi (at home, via SSH or keyboard)

```bash
# from the laptop, Pi on its USB port (card made with --ssh / install-sd.sh):
ssh -i ~/.config/mlpi/ssh/id_ed25519 mlpi@192.168.7.2
systemctl status mlpi.target 'mlpi*'
cat /var/lib/mlpi/sessions/current/summary.txt
journalctl -b -u mlpi-gadget -u mlpi
ls /sys/class/udc                 # empty → no gadget controller (see below)
# If empty: /boot/firmware/config.txt needs, under a plain [all] section,
#   dtoverlay=dwc2,dr_mode=peripheral
# A dwc2 line under [cm4]/[cm5]/[pi5] does NOT count on a Pi Zero 2 W.
# prepare-sd.sh adds it; if you edited config.txt by hand, check the section.
```
