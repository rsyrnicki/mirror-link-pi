# Known gaps

Documented gaps that the current code does **not** solve. Each one needs evidence
from a real car session before we burn time on a fix.

## 1. CCC certificate expiry (probably the biggest blocker)

**Empirical:** Daniel and Robert tried connecting old MirrorLink-certified Android
phones (Galaxy S6/S7 era) to Robert's car. They no longer pair. The Car Connectivity
Consortium dissolved MirrorLink around 2020-2021, original cert chains expired roughly
2023-2024, and the head unit's trust store rejects them.

Our DIY device has no CCC cert at all. The head unit will likely refuse the TLS-PSK
handshake at some point in the SOAP exchange.

**Investigate later, in this order:**
1. Capture every SOAP request the head unit makes before it gives up — the `Unhandled`
   warnings in `mlpi.log` are the data set.
2. Check if the head unit has a developer / engineering mode that disables cert checks
   (some Volkswagen, Ford, and Pioneer units do).
3. Consider a firmware downgrade to a build from before CCC enforcement (~2018).
4. As a last resort, look into MITM-ing the head unit and injecting a self-signed cert
   if its trust store is mutable.

**Don't:** burn time guessing at TLS-PSK setups before we have data on what the head
unit actually demands.

## 2. CCC-RFB Extensions not implemented

ETSI TS 103 310-4 defines pseudo-encodings for context info, key event injection,
content categorization, etc. None of the open-source VNC servers (x11vnc, TigerVNC,
wayvnc) implement these.

**Today:** plain RFB via x11vnc. Stock head units may negotiate down to a mode they
then refuse to display.

**Future work:** fork TigerVNC and add the pseudo-encodings. Roughly a week of work.

## 3. USB VID/PID is a guess

`scripts/pi-setup-gadget.sh` defaults to `0x1d6b:0x0104` (Linux Foundation /
Multifunction Composite Gadget). Some MirrorLink head units silently filter against an
internal whitelist of certified vendors:

| Vendor | VID    |
|--------|--------|
| Samsung | 0x04e8 |
| HTC     | 0x0bb4 |
| LG      | 0x1004 |
| Sony    | 0x054c |

Override via `MLPI_USB_VID` / `MLPI_USB_PID` env vars (or a systemd drop-in). See
[`pi-deployment.md` §6](pi-deployment.md). Note that impersonating another
manufacturer's device is a grey legal area in some jurisdictions.

## 4. Single USB function only

We expose one CDC-NCM function. Some head units expect a composite device with
multiple interfaces (NCM + ACM serial for control, or NCM + mass-storage). If a
silent-rejection problem persists after VID/PID iteration, try adding a dummy ACM
function alongside NCM.

## 5. No SSDP `byebye` on USB unplug

`SsdpResponder._send_byebye` runs on graceful shutdown (SIGTERM) but not when the USB
cable is yanked. In practice the head unit times out via `max-age` (1800 s default).
Could be improved with a netlink listener on `usb0` carrier state.

## 6. `netifaces` dropped

Robert's code used `netifaces`. It's unmaintained and won't build on Python ≥3.13.
Replaced by `subprocess` + `ip -j addr`, which needs `iproute2` (default on Pi OS
Bookworm and Fedora).

## 7. Python 3.11 minimum

`tomllib` requires Python ≥3.11. Pi OS Bookworm ships 3.11.2 — fine. If we ever need
to support Bullseye, vendor `tomli` instead.

## 8. NetworkManager fights with the stand-in interface (laptop only)

On Fedora, NetworkManager will auto-configure any new ethernet interface. The
`laptop-setup-stand-in.sh` script disables NM management for the chosen iface, but
**only for the current boot.** A reboot reverts this. For long-term dev, add a NM
keyfile under `/etc/NetworkManager/conf.d/`.

## 9. Multicast routing on multi-NIC hosts

Documented in `laptop-dev.md`. The setup script adds an explicit
`ip route replace 239.0.0.0/8 dev <iface>` to force SSDP through the right NIC.
