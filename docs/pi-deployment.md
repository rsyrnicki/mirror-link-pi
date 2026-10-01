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

Laptop requirements: Linux (tested on Fedora and Ubuntu), `sudo`, Python ≥ 3.11, the
repo checked out, and for phone mode `adb` + `qemu-user-static`
(Debian/Ubuntu: `sudo apt install adb qemu-user-static`;
Fedora: `sudo dnf install android-tools qemu-user-static`).

## 1. Flash the card — Raspberry Pi Imager

1. Device: *Raspberry Pi Zero 2 W*. OS: *Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)*.
2. Customisation ("Edit settings"):
   - **set a username and password** (needed only to log in for debugging at home),
   - Wi-Fi: optional (handy for SSH at home; irrelevant in the car),
   - enable SSH: optional,
   - **do not** enable any "USB gadget mode" option — MirrorLink-Pi sets up its own gadget.
3. Write. When Imager is done, take the card out and put it back in (so both partitions
   show up again).

## 2. Install MirrorLink-Pi onto the card

```bash
lsblk                                   # find the card, e.g. /dev/sdb or /dev/mmcblk0
sudo ./scripts/prepare-sd.sh /dev/sdX   # the whole device, not a partition
# or, with phone mode (pair the phone first: see phone-mode.md):
sudo ./scripts/prepare-sd.sh --phone /dev/sdX
```

Double-check the device name with `lsblk` (size, removable): the script writes to it.

If your desktop mounted the partitions already, the script unmounts and remounts
them itself. Alternatively: `sudo ./scripts/prepare-sd.sh --boot /run/media/$USER/bootfs --root /run/media/$USER/rootfs`.

What it writes:

| Where | What |
|---|---|
| rootfs `/opt/mlpi/` | the code (src, config, systemd, scripts, docs) + `VERSION` |
| rootfs `/etc/systemd/system/` | `mlpi.target` + 5 units, enabled at boot |
| rootfs `/etc/mlpi/mlpi-self-signed.crt` | our self-signed test cert served on `/cert/` |
| rootfs `/etc/NetworkManager/conf.d/99-mlpi-usb0.conf` | NetworkManager leaves `usb0` alone |
| rootfs `/etc/systemd/journald.conf.d/mlpi.conf` | persistent journal |
| rootfs `rpi-usb-gadget-ics.service` → masked | Pi OS's own USB-gadget helper can't grab `usb0` |
| bootfs `config.txt` | `dtoverlay=dwc2,dr_mode=peripheral` (USB device mode) |
| bootfs `mlpi.toml` | settings you can edit from any OS (see below) |

Re-running the script updates the code on the card and keeps recorded sessions.

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

```bash
sudo ./scripts/collect-logs.sh /dev/sdX          # → ./car-logs/<timestamp>/REPORT.txt
```

Or over SSH at home: `scp -r <user>@<pi>:/var/lib/mlpi/sessions .`

## Troubleshooting on the Pi (at home, via SSH or keyboard)

```bash
systemctl status mlpi.target 'mlpi*'
cat /var/lib/mlpi/sessions/current/summary.txt
journalctl -b -u mlpi-gadget -u mlpi
ls /sys/class/udc                 # empty → no gadget controller (see below)
# If empty: /boot/firmware/config.txt needs, under a plain [all] section,
#   dtoverlay=dwc2,dr_mode=peripheral
# A dwc2 line under [cm4]/[cm5]/[pi5] does NOT count on a Pi Zero 2 W.
# prepare-sd.sh adds it; if you edited config.txt by hand, check the section.
```
