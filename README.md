# MirrorLink-Pi

Make a Raspberry Pi Zero appear as a MirrorLink phone to a car head unit, so the car displays the Pi's screen via VNC over USB.

> **Status:** scaffolding + UPnP layer. USB-gadget, VNC and the CCC-cert handshake are still open. See [`docs/known-gaps.md`](docs/known-gaps.md).

## How it works

Per ETSI TS 103 388, the Pi acts as the *MirrorLink Server* (the role usually played by the phone). The car is the *MirrorLink Client*.

```
[Car head unit]  --USB--  [Pi: USB-NCM gadget @ usb0 / 192.168.7.2]
       |                       │
       |                       ├── DHCP (dnsmasq) → 192.168.7.10–50
       |                       ├── UPnP SSDP (1900/udp)  →  TmServerDevice:1
       |                       ├── HTTP (8080)  →  device descriptor + SCPDs
       |                       └── VNC (5900) → x11vnc on Pi's Xorg
```

## Repo layout

| Path | What |
|---|---|
| `src/mlpi/` | Python package: SSDP responder, HTTP descriptor, runner |
| `config/` | Device descriptor template, SCPD stubs, `mlpi.toml.example` |
| `scripts/` | Idempotent bash for Pi setup (gadget, dnsmasq, vnc) and laptop dev stand-in |
| `systemd/` | Units for production deployment on the Pi |
| `docs/` | Deployment guide, dev workflow, known gaps |
| `legacy/` | Robert's original files, kept for git-blame lineage |

## Quick start

**On the laptop (development):**
```bash
sudo ./scripts/laptop-setup-stand-in.sh <usb-eth-iface>   # configures USB-Eth as usb0 stand-in
MLPI_INTERFACE=<usb-eth-iface> python -m mlpi run --config config/mlpi.toml.example
```

**On the Pi (production):**
```bash
sudo ./scripts/pi-install.sh
sudo systemctl enable --now mlpi.target
```

See [`docs/laptop-dev.md`](docs/laptop-dev.md) and [`docs/pi-deployment.md`](docs/pi-deployment.md) for details.

## License

GPL-3.0 — see [`LICENSE`](LICENSE). Carried from Robert's original repo at https://github.com/rsyrnicki/mirror-link-pi.
