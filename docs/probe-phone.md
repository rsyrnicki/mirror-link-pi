# Probing a real MirrorLink phone

A certified MirrorLink phone (e.g. a Galaxy S6) is the best reference we have: it is a
real MirrorLink server. `mlpi probe-phone` drives it from a Linux laptop the way the
car would, and records what it sends — so we can compare a working server against ours
and see, at each step, what a head unit really expects and enforces.

This records **public protocol data from a device you own**. It does not extract or
copy any private key, and a copied DAP response cannot be replayed against a car (the
car picks a fresh random nonce every time). What it *can* answer:

- What does a certified server's device description, app list and DAP response look
  like (categories, trust levels, `X_Signature`)?
- Are the phone's certificates **expired**? `certs.txt` shows their validity dates —
  the likely reason old certified phones stopped pairing.
- Does the phone's VNC handshake differ from ours?

## What you need

- The phone, with MirrorLink enabled in its settings (Galaxy S6: Settings → look for
  "MirrorLink"; it only appears switched on while a MirrorLink client is connected).
- A **Linux** laptop, run as **root** (USB control transfer + configuring the network
  interface). `openssl` optional but recommended (decodes the certificates).
- A USB-data cable from the phone to the laptop.

## Run it

```bash
# See what's attached and identify the phone:
sudo PYTHONPATH=src python3 -m mlpi probe-phone --list

# Probe it (auto-detects known MirrorLink vendors — Samsung, LG, HTC, Sony, …):
sudo PYTHONPATH=src python3 -m mlpi probe-phone

# Or target a specific device / vendor / MirrorLink version:
sudo PYTHONPATH=src python3 -m mlpi probe-phone --device 1:23
sudo PYTHONPATH=src python3 -m mlpi probe-phone --vendor 0x04e8 --version 1.1
```

It will:

1. Send the **MirrorLink USB command** to wake the phone into MirrorLink mode.
2. Wait for the phone's USB network interface, and get an address from **its** DHCP
   server (the phone is the server here — the roles are reversed from the car case).
3. **SSDP**-discover the phone and fetch its device description and SCPDs.
4. `SetClientProfile` (we are the client now), then `GetApplicationList`.
5. Launch each advertised app: run **DAP** and save its real certificates; connect to
   its **VNC** server and screenshot the phone's MirrorLink screen.

## What you get

Recorded under `<session-root>/probe-phone/<timestamp>/`:

| File | Content |
|---|---|
| `device-description.xml` | the phone's UPnP device XML — versions, `X_Signature`, service list |
| `scpd/*.xml` | its service descriptions |
| `app-list.xml` | its `GetApplicationList` — which apps, categories, trust levels |
| `dap/attestation.xml` | its DAP `attestationResponse` |
| `dap/*.der`, `dap/certs.txt` | the certificates it sends, decoded (subjects, issuers, **validity dates**, fingerprints) |
| `phone-screen.png` | a VNC screenshot of the phone's screen |
| `vnc-rx.bin` | the raw VNC stream (so nothing is lost if our decoder trips) |
| `events.jsonl`, `report.txt` | the full timeline |

## If it doesn't work

- **No new network interface** after the USB command → the phone did not switch to
  MirrorLink mode. Enable MirrorLink in its settings and try again; some phones only
  offer it once a client has issued the command, so a second run can succeed.
- **No SSDP response** → the phone came up on the network but isn't advertising UPnP;
  check `ip addr` shows the new interface with an address in `192.168.x.y`.
- **VNC needs auth** → the phone offered a security type other than None; the raw
  stream is still saved. (MirrorLink itself mandates None, Part 2 §6.2.)

## Then what

Compare the phone's files with ours. If its DAP certificates are expired, that
confirms the certificate wall is expiry, not our implementation — and no spoof helps,
because the car checks the same dates (unless it has no clock; Part 4 §5). If the
phone gets a picture in your car but our Pi doesn't, the difference between their
`device-description.xml`/`app-list.xml` and ours is the next thing to try.
