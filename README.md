# MirrorLink-Pi

Your Android phone's apps on a MirrorLink car screen. A Raspberry Pi Zero 2 W plugged
into the car's USB socket presents itself as a MirrorLink phone; the head unit shows
Google Maps, Spotify or any other app from your phone, and you control them by touch.

![Google Maps from an Android phone on a VW Polo's MIB2 head unit, through MirrorLink-Pi](docs/img/car-phone-maps.jpg)

*Google Maps navigation from the phone on the car's screen (street names blurred).*

**Status: works in daily use** with a VW Polo's MIB2 Standard head unit (`VW-Mibstd2`)
and a Samsung Galaxy A56 (Android 16). Other MirrorLink head units and Android phones
are untested so far; see [`docs/compatibility.md`](docs/compatibility.md) and please
add yours.

## What it does

- **Phone apps on the car screen:** about 20 frames per second at 800×480, touch input,
  a delay of roughly a quarter of a second. Apps run on a separate car-sized display
  inside the phone, so the phone's own screen can stay off.
- **Launcher with big tiles** for your favourite apps, plus all installed apps.
- **Status bar from the phone:** clock, battery (with charging), mobile signal and
  network type, media buttons (previous, play/pause, next), Do Not Disturb, and a switch
  for the phone's own screen.
- **Back and Home buttons** on top of the phone picture (the MIB2 sends no hardware
  keys to MirrorLink).
- **Internet stays on the phone's mobile data;** the Pi checks that and warns on the car
  screen if the phone routes its traffic to the Pi's Wi-Fi instead. **Audio** stays on
  the phone's normal Bluetooth connection to the car.
- **Pairing on the car screen:** if the phone doesn't know the Pi, type Android's pairing
  code on a number pad in the car.
- **One-command SD card setup** (`install-sd.sh`), **updates and log collection over the
  USB cable** without taking the card out, and protection against power cuts when the
  ignition goes off.
- No app on the phone: it uses Android's built-in Wireless debugging and
  [scrcpy](https://github.com/Genymobile/scrcpy).

**Limits:** the head unit blocks the picture while the car is moving, because the Pi
isn't CCC-certified (see below); use it parked. Android only: iPhones have no
equivalent of Wireless debugging. Not yet tried in the car: typing with the car's
on-screen keyboard and the experimental Bluetooth audio auto-connect.

## How it got here

The first connection, 2026-09-29: the car lists the Pi as a MirrorLink app, launches it
and shows the Pi's screen, with touch input coming back to the Pi.

| The car lists the Pi as a MirrorLink app | The Pi's screen on the car's display |
|---|---|
| ![MirrorLink-compatible apps: MirrorLink Pi](docs/img/car-app-list.jpg) | ![MirrorLink-Pi status screen on the head unit](docs/img/car-screen.jpg) |
| **Touches reach the Pi (orange crosses), Pi Zero 2 W on the seat** | **Blocked while driving: the Pi is not CCC-certified** |
| ![Touch markers on the car screen, Pi Zero 2 W connected by USB](docs/img/car-touch-and-pi.jpg) | !["The mobile device is restricted."](docs/img/car-restricted.jpg) |

What made it work: the winning protocol variant is **`s6-audio-home`**. Its app list is
shaped like a real Galaxy S6's (probed with `mlpi probe-phone`, see
[`docs/probe-phone.md`](docs/probe-phone.md)): a VNC home-screen app plus RTP audio
server/client entries for the payload types 98/99 the car announces. With a bare VNC
entry the car stopped right after `GetApplicationList`. Every car trip was an automated
experiment: the Pi records everything and rotates through protocol variants until the
car connects, then locks the winner (see [`docs/field-test.md`](docs/field-test.md)).
The implementation follows the public MirrorLink spec, ETSI TS 103 544 (see
[`docs/spec-notes.md`](docs/spec-notes.md)). Phone mode followed on 2026-10-01
(version 1.0); the status bar, pairing in the car and the rest since then
([`CHANGELOG.md`](CHANGELOG.md)).

## Legal and safety notice

- **Research project, not a product.** Non-commercial interoperability research on a
  discontinued protocol. No warranty of any kind (see [`LICENSE`](LICENSE)); you use it
  at your own risk.
- **Not affiliated with or endorsed by** the Car Connectivity Consortium (CCC),
  Volkswagen, Samsung or any other company. "MirrorLink" is a trademark of the CCC and
  is used here only to describe what the software is compatible with. MirrorLink-Pi is
  **not MirrorLink-certified** and does not claim to be.
- **Written from the public spec.** The implementation follows the publicly available
  ETSI TS 103 544. The spec itself is not included (`scripts/fetch-spec.sh` downloads
  it for personal reference).
- **No keys, certificates or protection circumvention.** The repository contains no
  CCC or manufacturer keys or certificates, and nothing that defeats device attestation
  or the car's driver-distraction lock. The only certificate is our own self-signed test
  certificate (`config/mlpi-self-signed.crt`).
- **Do not operate it while driving.** The head unit blocks uncertified content while
  the car moves, by design. Test while parked, and do not try to get around that lock.
- **Your car, your responsibility.** Connecting unofficial devices to a vehicle may
  affect its warranty. Captures in this repo have had vehicle identifiers removed
  (`scripts/scrub-captures.py`).
- **Phone mode changes settings on your phone over adb, and that can be risky.** It
  needs Wireless debugging switched on, and the Pi keeps an adb key that grants full
  shell access to the phone: anyone who has the SD card (or the laptop key in
  `~/.config/mlpi/adb/`) and can reach the phone's Wireless debugging port can control
  it. Revoke it any time under *Developer options → Revoke USB debugging
  authorizations*. While connected, the Pi:
  - sets Android's "avoid bad Wi-Fi" (`network_avoid_bad_wifi=1`), which **stays set**
    afterwards (turn off with `avoid_bad_wifi = false`; undo with
    `adb shell settings delete global network_avoid_bad_wifi`);
  - copies the scrcpy server into `/data/local/tmp` (`mlpi-scrcpy-list.jar` stays
    there), creates a virtual display, keeps the phone awake and turns its own screen
    off until the connection ends.

  Use phone mode only with a phone you own, and switch Wireless debugging off when you
  don't need it.

## How it works

Per ETSI TS 103 544 / CCC MirrorLink, the Pi is the *MirrorLink Server* (normally the
phone), the car is the *MirrorLink Client*.

```
[Car head unit] ──USB── [Pi Zero 2 W: CDC-NCM gadget, usb0 = 192.168.7.2]
                          ├── USB vendor request 0xF0 (MirrorLink USB command) → ACK
                          ├── DHCP        (67/udp)   → car gets 192.168.7.44
                          ├── SSDP        (1900/udp) → TmServerDevice:1
                          ├── HTTP/SOAP   (8080)     → descriptor, SCPDs, control, GENA events
                          ├── VNC / RFB   (5900)     → status screen, MirrorLink VNC extensions
                          ├── DAP         (5510)     → honest "attestation not available"
                          ├── pcap + journal + event log → /var/lib/mlpi/sessions/NNNN/
                          └── green LED   → how far the car got (no screen needed)
```

Everything is pure Python standard library on stock Raspberry Pi OS Lite, so the SD
card is prepared completely on the laptop and the Pi never needs internet.

## Phone mode (scrcpy)

The Pi mirrors an Android phone over its own Wi-Fi hotspot with
[scrcpy](https://github.com/Genymobile/scrcpy): the phone renders an 800×480 virtual
display, the Pi decodes it and sends it to the car through the MirrorLink session above,
and car touches go back to the phone. The Pi draws its own launcher with big tiles for
your favourite apps. Internet stays on the phone's mobile data; audio stays on the
phone's Bluetooth link to the car. See [`docs/phone-mode.md`](docs/phone-mode.md).

## Quick start

You need: a **Raspberry Pi Zero 2 W**, a microSD card (8 GB or more), a micro-USB
**data** cable (USB-A or USB-C to micro-USB, to match the car's socket), a **Linux
laptop**, and for phone mode an **Android phone** (Android 11 or newer, for Wireless
debugging).

```bash
# 0. On the laptop, once (Python 3.11+; Debian/Ubuntu package names,
#    Fedora: sudo dnf install android-tools qemu-user-static python3-tkinter openssl):
sudo apt install adb qemu-user-static python3-tk openssl
git clone https://github.com/rsyrnicki/mirror-link-pi && cd mirror-link-pi
# 1. Make the SD card: downloads Raspberry Pi OS Lite, writes it, sets everything up
#    (phone mode, updates over USB, power-cut protection; login mlpi / mlpi):
lsblk                                         # find the card, e.g. /dev/sdb
sudo ./scripts/install-sd.sh /dev/sdX
# 2. Phone: Developer options → Wireless debugging on; join the Wi-Fi "MirrorLink-Pi"
#    (password printed by step 1). When it warns "no internet", don't answer: go to
#    the home screen. The first time, the car asks for Android's pairing code.
# 3. Pre-flight at home: card in the Pi, Pi's USB port → laptop, wait for 2 LED blinks:
PYTHONPATH=src python3 -m mlpi simulate-car --target 192.168.7.2
PYTHONPATH=src python3 -m mlpi car-view       # live window, like the car's screen
# 4. Car: plug the Pi into the car's USB socket, open "MirrorLink Pi" on the head unit.
# 5. Back home, if something went wrong (Pi on the laptop's USB port, card stays in):
./scripts/collect-logs.sh --pi                # or, card in the laptop: sudo ... /dev/sdX
# Later updates without taking the card out (Pi on the laptop's USB port):
./scripts/update-pi.sh
```

Step by step, with what to expect at each point:
[`docs/pi-deployment.md`](docs/pi-deployment.md) (SD card),
[`docs/phone-mode.md`](docs/phone-mode.md) (phone setup and use),
[`docs/field-test.md`](docs/field-test.md) (pre-flight and the car).

Tested cars and phones, and how to add yours:
[`docs/compatibility.md`](docs/compatibility.md).

How the Pi and the car communicate, step by step: [`docs/how-it-works.md`](docs/how-it-works.md).

More: [`docs/laptop-dev.md`](docs/laptop-dev.md) (development and tests),
[`docs/spec-notes.md`](docs/spec-notes.md) (the MirrorLink spec, clause by clause),
[`docs/probe-phone.md`](docs/probe-phone.md) (measuring a real MirrorLink phone),
[`docs/known-gaps.md`](docs/known-gaps.md) (what we know we don't know).

## Repo layout

| Path | What |
|---|---|
| `src/mlpi/` | the server: `dhcp`, `ssdp`, `http_descriptor` + `soap` + `eventing`, `rfb` + `mirrorlink_vnc` + `canvas` + `screen`, `dap`, `variants`, `session`, `capture`, `gadget`, `led`, `runner`; phone mode: `phone` (scrcpy client), `avdecode` (H.264 via libavcodec), `video` (frames + source switch), `launcher` + `phonestatus` (home screen, status bar), `pairing` (number pad), `btaddr`; `health` (Pi temperature, power, Wi-Fi link) |
| `src/mlpi/tools/` | `simulate_car` (recorded VW handshake + VNC client), `car_view` (live window that behaves like the car), `probe_phone` (drive a real phone), `phone_tools` (`pair-phone`, `phone-preview`), `report`, `discover` |
| `config/` | device descriptor template, SCPDs, `variants.toml`, `mlpi.toml.example` |
| `systemd/` | units started at boot on the Pi |
| `scripts/` | laptop: `install-sd.sh` (download + write + set up), `prepare-sd.sh`, `make-data-partition.sh`, `update-pi.sh`, `collect-logs.sh`, `fetch-spec.sh`, `fetch-scrcpy-server.sh`, `scrub-captures.py`; on the Pi: `apply-update.sh`, `boot-check.sh`; `probe-sai-*` (VW SAI research) |
| `captures/` | car captures from earlier sessions (vehicle identifiers scrubbed) |
| `legacy/` | Robert's original files, kept for git-blame lineage |

## License

GPL-3.0 — see [`LICENSE`](LICENSE). Carried from Robert's original repo at https://github.com/rsyrnicki/mirror-link-pi.
Provided "as is", without warranty; see the Legal and safety notice above. Trademarks
belong to their owners.
