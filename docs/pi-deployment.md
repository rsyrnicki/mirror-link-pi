# Pi deployment

Target: Raspberry Pi Zero 2W, Pi OS Bookworm (32-bit Lite or Full).

## 0. Flash

Use Raspberry Pi Imager. Pre-configure SSH, user, WiFi in the customise dialog so you can SSH in headless.

## 1. Enable USB OTG (one-time)

The Pi Zero 2W's USB-OTG port becomes a peripheral only when the `dwc2` overlay is set:

```bash
sudo sh -c 'echo "dtoverlay=dwc2,dr_mode=peripheral" >> /boot/firmware/config.txt'
sudo sh -c 'echo "dwc2" >> /etc/modules'
sudo reboot
```

After reboot, verify the UDC is exposed:

```bash
ls /sys/class/udc       # must list at least one entry, typically "20980000.usb"
```

If empty, the overlay didn't take — recheck `/boot/firmware/config.txt`.

## 2. Optional: WiFi driver fix (Pi Zero 2W only)

Some Pi Zero 2W units have a flaky brcmfmac driver that drops WiFi every ~5 minutes:

```bash
echo "options brcmfmac feature_disable=0x2000" | sudo tee /etc/modprobe.d/brcmfmac.conf
sudo reboot
```

(Originally documented by Robert in `legacy/PREPAREPI.md`.)

## 3. Install MirrorLink-Pi

```bash
git clone https://github.com/rsyrnicki/mirror-link-pi.git
cd mirror-link-pi
sudo ./scripts/pi-install.sh
```

This:
- installs `dnsmasq`, `x11vnc`, `python3-venv`
- copies the repo to `/opt/mlpi/`
- creates `/opt/mlpi/.venv/` and pip-installs the package editable
- writes `/etc/mlpi/mlpi.toml` from the example
- installs systemd units to `/etc/systemd/system/`
- ensures `dtoverlay=dwc2` is in `config.txt`

## 4. Tune `/etc/mlpi/mlpi.toml`

The defaults already point at `usb0`/`192.168.7.2`/8080. The two values you may want to override:

- `[ssdp].notify_interval_seconds` — drop to 60 while debugging so head-unit re-discovery is faster
- `[device].friendly_name` — what the car shows in its menu

## 5. Start

```bash
sudo systemctl enable --now mlpi.target
journalctl -u mlpi-upnp.service -f      # watch SOAP requests from the head unit
journalctl -u dnsmasq-usb0.service -f   # watch DHCP leases
```

Plug the USB-OTG cable into the car. Within 5–10 s you should see (in `journalctl`):

1. `dnsmasq-usb0`: `DHCPACK ... 192.168.7.10 ...`
2. `mlpi-upnp`: `M-SEARCH from ('192.168.7.10', NNNN)`
3. `mlpi-upnp`: `GET / HTTP/1.1 200`
4. likely a stream of `Unhandled POST /ctrl/...` warnings — **this is the goal**, every one teaches us what the head unit expects.

## 6. Override USB descriptors (when the car ignores us)

If step 5 shows DHCP leases but no M-SEARCH, the head unit is silently filtering our USB descriptor. Try a known-MirrorLink-vendor VID:

```bash
sudo systemctl stop mlpi.target
sudo MLPI_USB_VID=0x04e8 MLPI_USB_PID=0x6860 /opt/mlpi/scripts/pi-setup-gadget.sh   # Samsung S5
sudo systemctl start mlpi-upnp.service mlpi-vnc.service dnsmasq-usb0.service
```

To make the override permanent, add the env vars to a drop-in:

```bash
sudo systemctl edit mlpi-gadget.service
# In the editor:
# [Service]
# Environment=MLPI_USB_VID=0x04e8
# Environment=MLPI_USB_PID=0x6860
```

See [`known-gaps.md`](known-gaps.md) for the legal/empirical caveats.

## 7. Reset

```bash
sudo systemctl disable --now mlpi.target
sudo rm -rf /opt/mlpi /etc/mlpi /var/log/mlpi
sudo rm /etc/systemd/system/{mlpi-*.service,mlpi.target,dnsmasq-usb0.service}
sudo rm /etc/dnsmasq.d/usb0.conf
sudo systemctl daemon-reload
```
