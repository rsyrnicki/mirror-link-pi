# Testing

## Unit + end-to-end tests (laptop, CI)

```bash
pip install pytest
PYTHONPATH=src python3 -m pytest
```

| Test file | Covers |
|---|---|
| `test_config.py` | defaults, file/env overrides, type coercion |
| `test_netinfo.py` | `ip -j addr` parsing (mocked subprocess) |
| `test_ssdp.py` | alive/byebye/M-SEARCH rendering |
| `test_http_descriptor.py` | descriptor template, XML escaping, SCPDs, per-variant `X_mirrorLinkVersion` |
| `test_soap.py` | real car envelopes, **single** escaping of ClientProfile, app list per variant, launch/status |
| `test_dhcp.py` | DISCOVER/OFFER/REQUEST/ACK/NAK, options, address pool |
| `test_canvas.py` | font, pixel formats (RGB565, 32 bpp BE, colour map), dirty tracking |
| `test_variants.py` | rotation per attempt, locking/persisting the winner, simulator never locks |
| `test_session.py` | boot counter, sticky stages, summary |
| `test_mirrorlink.py` | MirrorLink VNC message layouts (Part 2 tables), DAP stub |
| `test_gadget.py` | FunctionFS descriptor/strings blobs, USB command parsing, ACK/STALL |
| `test_usbhost.py` | host-side MirrorLink USB command struct + ioctl layout |
| `test_probe_phone.py` | descriptor/URI parsing, DAP certificate extraction, DHCP client packets |
| `test_rfb.py` | RFB 3.3/3.7 clients, colour-map clients, MirrorLink extension + unknown messages recorded |
| `test_end_to_end.py` | real HTTP + VNC servers on localhost driven by the car simulator (full MirrorLink VNC handshake, context info, device status, ByeBye) |

## Pre-flight on real hardware (home)

[`field-test.md` → Pre-flight](field-test.md#pre-flight-at-home): Pi on the laptop's USB
port, `mlpi simulate-car --target 192.168.7.2`, check `car-view.png`. Covers the
things the unit tests cannot: USB gadget enumeration, DHCP over the real link,
NetworkManager staying away from `usb0`, systemd ordering, LED.

## Only the car can tell

- whether the head unit opens the VNC connection for any variant
- which RFB/MirrorLink VNC extensions it then requires (recorded raw in `vnc-N-rx.bin`)
- whether CCC certification (DAP, signatures) is enforced — see [`known-gaps.md`](known-gaps.md)
