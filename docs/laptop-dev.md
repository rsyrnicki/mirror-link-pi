# Laptop development

Target: any Linux laptop with iproute2, Python ≥3.11, and a USB-Ethernet adapter.

The laptop **cannot act as a USB gadget** (the SoC's xHCI is host-only on every consumer laptop we've checked). Instead, a USB-Ethernet adapter is configured to look like the Pi's `usb0` interface so the Python stack can be exercised end-to-end.

## Setup

```bash
# 1. Find the adapter
nmcli -t -f DEVICE,TYPE device | grep ethernet
# Example output:  enp0s20f0u1:ethernet

# 2. Configure it as the usb0 stand-in
sudo ./scripts/laptop-setup-stand-in.sh enp0s20f0u1

# 3. Install the package
pip install -e '.[dev]'

# 4. Run the service against the stand-in
MLPI_INTERFACE=enp0s20f0u1 python -m mlpi run --config config/mlpi.toml.example
```

## Verify

In separate terminals:

```bash
# Discovery — should print 192.168.7.2
python -m mlpi discover --interface enp0s20f0u1 --verbose

# Descriptor
curl -s http://192.168.7.2:8080/ | xmllint --format -

# Each SCPD
for s in ApplicationControl ServerProfile ClientProfile NotificationService ProfileService; do
    curl -s "http://192.168.7.2:8080/scpd/$s.xml" | xmllint --noout && echo "$s OK"
done

# Unhandled SOAP — should return 501 and write a WARNING to the log
curl -X POST http://192.168.7.2:8080/ctrl/ApplicationServer \
     -H 'Content-Type: text/xml' -d '<envelope/>'
tail $XDG_STATE_HOME/mlpi/mlpi.log    # or ~/.local/state/mlpi/mlpi.log
```

## No adapter? Loopback fallback

A self-contained smoke test that uses no special hardware:

```bash
cat > /tmp/mlpi-test.toml <<'EOF'
[network]
interface = "lo"
address   = "127.0.0.1"
http_port = 8088

[ssdp]
multicast_ttl = 0
notify_interval_seconds = 5
EOF

python -m mlpi run --config /tmp/mlpi-test.toml &
sleep 2
curl -s http://127.0.0.1:8088/ | xmllint --noout && echo "XML OK"
python -m mlpi simulate-car --config /tmp/mlpi-test.toml --target 127.0.0.1
kill %1
```

## Cleanup

```bash
sudo ip addr flush dev enp0s20f0u1
sudo nmcli device set enp0s20f0u1 managed yes
```

## Multicast routing gotcha

With WiFi up at metric 600 and the USB-Eth adapter at metric 100, Linux still sometimes
routes 239.0.0.0/8 via WiFi for the *first* packet of a session. The setup script adds
an explicit `ip route replace 239.0.0.0/8 dev <iface>` to nail this down. If you skip
the script, you may see `discover` time out even though the responder is running.
