# MirrorLink-Pi

Make a Raspberry Pi Zero 2 W appear as a MirrorLink phone to a car head unit, so the
car displays a screen rendered by the Pi (over VNC, over USB).

## First success — 2026-09-29

A VW Polo's MIB2 Standard head unit (`VW-Mibstd2`) lists the Pi as a MirrorLink app,
launches it and shows the Pi's screen, with touch input coming back to the Pi.

| The car lists the Pi as a MirrorLink app | The Pi's screen on the car's display |
|---|---|
| ![MirrorLink-compatible apps: MirrorLink Pi](docs/img/car-app-list.jpg) | ![MirrorLink-Pi status screen on the head unit](docs/img/car-screen.jpg) |
| **Touches reach the Pi (orange crosses), Pi Zero 2 W on the seat** | **Blocked while driving: the Pi is not CCC-certified** |
| ![Touch markers on the car screen, Pi Zero 2 W connected by USB](docs/img/car-touch-and-pi.jpg) | !["The mobile device is restricted."](docs/img/car-restricted.jpg) |

What made it work: the winning protocol variant is **`s6-audio-home`**. Its app list is
shaped like a real Galaxy S6's (probed with `mlpi probe-phone`, see
[`docs/probe-phone.md`](docs/probe-phone.md)): a VNC home-screen app plus RTP audio
server/client entries for the payload types 98/99 the car announces. With a bare VNC
entry the car stopped right after `GetApplicationList`. Every car trip is an automated
experiment: the Pi records everything and rotates through protocol variants until the
car connects, then locks the winner (see [`docs/field-test.md`](docs/field-test.md)).
The implementation follows the public MirrorLink spec, ETSI TS 103 544 (see
[`docs/spec-notes.md`](docs/spec-notes.md)).

Known limit: while the car is moving, the head unit blocks the picture because the Pi
cannot pass the CCC certification check (device attestation needs a CCC-issued key).

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

## Quick start

```bash
# 1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager (set a user).
# 2. Install onto the card, from the laptop:
sudo ./scripts/prepare-sd.sh /dev/sdX
# 3. Pre-flight at home: Pi's USB port → laptop, then
PYTHONPATH=src python3 -m mlpi simulate-car --target 192.168.7.2
# 4. Car. 5. Back home:
sudo ./scripts/collect-logs.sh /dev/sdX
```

Details: [`docs/pi-deployment.md`](docs/pi-deployment.md) (SD card),
[`docs/field-test.md`](docs/field-test.md) (the trip),
[`docs/laptop-dev.md`](docs/laptop-dev.md) (development),
[`docs/spec-notes.md`](docs/spec-notes.md) (the MirrorLink spec, clause by clause),
[`docs/probe-phone.md`](docs/probe-phone.md) (measuring a real MirrorLink phone),
[`docs/known-gaps.md`](docs/known-gaps.md) (what we know we don't know).

## Repo layout

| Path | What |
|---|---|
| `src/mlpi/` | the server: `dhcp`, `ssdp`, `http_descriptor` + `soap` + `eventing`, `rfb` + `mirrorlink_vnc` + `canvas` + `screen`, `dap`, `variants`, `session`, `capture`, `gadget`, `led`, `runner` |
| `src/mlpi/tools/` | `simulate_car` (recorded VW handshake + VNC client), `probe_phone` (drive a real phone), `report`, `discover` |
| `config/` | device descriptor template, SCPDs, `variants.toml`, `mlpi.toml.example` |
| `systemd/` | units started at boot on the Pi |
| `scripts/` | `prepare-sd.sh`, `collect-logs.sh`, `fetch-spec.sh` (laptop); `probe-sai-*` (VW SAI research) |
| `captures/` | car captures from earlier sessions, CCC reference material |
| `legacy/` | Robert's original files, kept for git-blame lineage |

## License

GPL-3.0 — see [`LICENSE`](LICENSE). Carried from Robert's original repo at https://github.com/rsyrnicki/mirror-link-pi.
