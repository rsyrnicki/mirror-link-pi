# MirrorLink-Pi

Make a Raspberry Pi Zero 2 W appear as a MirrorLink phone to a car head unit, so the
car displays a screen rendered by the Pi (over VNC, over USB).

> **Status (2026-09):** the VW MIB II ("VW-Mibstd2") talks to us through the whole UPnP
> handshake up to `LaunchApplication` + `GetApplicationStatus = Foreground`, but has
> never opened the VNC connection yet. The implementation now follows the public
> MirrorLink spec (ETSI TS 103 544 — see [`docs/spec-notes.md`](docs/spec-notes.md)),
> which revealed several things we did wrong, and every car trip is an automated
> experiment: the Pi records everything and rotates through protocol variants until
> the car connects. See [`docs/field-test.md`](docs/field-test.md).

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
[`docs/known-gaps.md`](docs/known-gaps.md) (what we know we don't know).

## Repo layout

| Path | What |
|---|---|
| `src/mlpi/` | the server: `dhcp`, `ssdp`, `http_descriptor` + `soap` + `eventing`, `rfb` + `mirrorlink_vnc` + `canvas` + `screen`, `dap`, `variants`, `session`, `capture`, `gadget`, `led`, `runner` |
| `src/mlpi/tools/` | `simulate_car` (recorded VW handshake + VNC client), `report`, `discover` |
| `config/` | device descriptor template, SCPDs, `variants.toml`, `mlpi.toml.example` |
| `systemd/` | units started at boot on the Pi |
| `scripts/` | `prepare-sd.sh`, `collect-logs.sh`, `fetch-spec.sh` (laptop); `probe-sai-*` (VW SAI research) |
| `captures/` | car captures from earlier sessions, CCC reference material |
| `legacy/` | Robert's original files, kept for git-blame lineage |

## License

GPL-3.0 — see [`LICENSE`](LICENSE). Carried from Robert's original repo at https://github.com/rsyrnicki/mirror-link-pi.
