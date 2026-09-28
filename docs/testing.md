# Testing

## Unit tests

```bash
pip install -e '.[dev]'
pytest -v
```

Coverage:
- `tests/test_config.py` — defaults, file overrides, env overrides, type coercion
- `tests/test_netinfo.py` — `ip -j addr` parsing (mocked subprocess)
- `tests/test_ssdp.py` — message rendering for alive/byebye/M-SEARCH/response
- `tests/test_http_descriptor.py` — descriptor template, XML escaping, SCPD existence

## End-to-end on the laptop

See [`laptop-dev.md`](laptop-dev.md) — the "Verify" section is the canonical checklist.

What we can verify on the laptop, with no Pi or car:

- [ ] `python -m mlpi run` starts cleanly (no traceback)
- [ ] `python -m mlpi simulate-car --target <addr>` gets HTTP/1.1 200 with our LOCATION
- [ ] `curl http://<addr>:8080/` returns valid XML (`xmllint --noout` accepts it)
- [ ] All five SCPD URLs return XML
- [ ] `POST /ctrl/ApplicationServer` returns 501; full request body is logged to mlpi.log
- [ ] `systemd-analyze verify systemd/*.service` passes (modulo "binary not on host" warnings)
- [ ] `bash -n scripts/*.sh` clean

## What we can't test without the Pi

- USB gadget enumeration on the head unit (needs a real UDC)
- DHCP lease handed to the head unit (needs the gadget-side `usb0`)
- Actual VNC display of the desktop

## What we can't test without the car

- Whether the head unit accepts our VID/PID
- Whether the head unit gets past TLS-PSK / CCC-cert handshake (see [`known-gaps.md`](known-gaps.md))
- Whether the head unit accepts the descriptor's service list
- Whether stock VNC suffices or CCC-RFB-Extensions are mandatory

## In the car: what to capture

Bring a laptop with `journalctl -f` running over SSH:

```bash
ssh pi@192.168.7.2 'journalctl -u mlpi-upnp.service -u dnsmasq-usb0.service -f' | tee car-session-$(date +%F).log
```

The "Unhandled POST" warnings are the gold — every one is a SOAP action the head
unit invoked. Save these logs; they drive the next implementation iteration.
