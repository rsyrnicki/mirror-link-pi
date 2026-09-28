# Laptop development

Everything except the USB gadget runs on any Linux laptop with Python ≥ 3.11. No
packages are needed (pytest only for the tests).

## Tests

```bash
pip install pytest          # or: pip install -e '.[dev]'
PYTHONPATH=src python3 -m pytest
```

`tests/test_end_to_end.py` starts the real HTTP/SOAP and VNC servers on localhost and
drives them with the car simulator (recorded VW handshake → LaunchApplication → VNC
frame → touch event).

## Running the server locally

The DHCP server and the capture need the `usb0` gadget interface, so switch them off
and bind to loopback:

```bash
cat > /tmp/mlpi-dev.toml <<'EOF'
[network]
interface = "lo"
address = "127.0.0.1"
http_port = 8088
vnc_port = 5909
[dhcp]
enabled = false
[led]
enabled = false
[watchdog]
idle_reconnect_seconds = 0
[session]
root = "/tmp/mlpi-dev"
EOF

PYTHONPATH=src python3 -m mlpi --config /tmp/mlpi-dev.toml run &
PYTHONPATH=src python3 -m mlpi simulate-car --target 127.0.0.1 --http-port 8088 --attempts 3
PYTHONPATH=src python3 -m mlpi report /tmp/mlpi-dev/sessions/current/
kill %1
```

A VNC viewer on `127.0.0.1:5909` shows the status screen.

## Rendering the screen without networking

```bash
PYTHONPATH=src python3 -m mlpi screenshot screen.png
```

## Analysing car captures

`python3 -m mlpi report <session-dir>` for the structured timeline; Wireshark on
`usb0.pcap` for the packets. Older captures (before this recorder existed) are in
`captures/`.
