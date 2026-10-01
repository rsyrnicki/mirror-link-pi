# Known gaps

What the current code does **not** solve, and what we know about it. Each entry should
be driven by evidence from a car session before we spend time on a fix.

## 1. The car launches our app but never connects to VNC (solved)

**Solved on 2026-09-29** by the `s6-audio-home` variant: an app list shaped like a real
Galaxy S6's (VNC home-screen app plus RTP audio entries). The history below is kept for
other head units that may behave differently.

**Evidence (2026-05-02, `launch-realcert.pcap`):** every ~10 s the MIB II runs
DHCP → descriptor → Get/SetClientProfile → GetApplicationList → SUBSCRIBE →
GetApplicationList → LaunchApplication → GetApplicationStatus(0x1) = Foreground →
NOTIFY → GetApplicationStatus(0x00000001) = Foreground → silence. No TCP SYN to 5900 in
any capture. From the second round on it fetches our icon, so it does list us.

**Fixed since:** SetClientProfile/GetClientProfile echoed the profile *double-escaped*
(`&amp;lt;clientProfile…`) — the car got text instead of its profile back.

**Found in the spec since** ([`spec-notes.md`](spec-notes.md)), all fixed:
- we STALLed the **MirrorLink USB command** (Part 1 §4.2.2) — per §4.2.3 that tells
  the car "no MirrorLink server here"; now answered via FunctionFS;
- the VNC server entry must have **appCategory 0xF0000001** (Part 9 §5.2.1); we sent
  0x00000000;
- `X_mirrorLinkVersion` was in the wrong XML namespace (Part 12 §5);
- a 1.1 client expects a **DAP** endpoint in the app list (Part 13 §7.3.4).

**Tested automatically by variant rotation** (`config/variants.toml`): ML 1.0 vs 1.1,
with/without DAP stub, with/without a home-screen app, plus the old session-3 listing
as a control group.

**If no variant works**, next candidates:
- RTP audio server entries (Part 9 §5.2.3; the car's ClientProfile announces RTP
  payloads 98/99, and Part 13 §7.3.5 says audio is set up at session start);
- the car may insist on successful DAP / `X_Signature` for 1.1 — not achievable
  without a CCC key; then only a 1.0 session is possible;
- a capture of any certified phone against any head unit would settle the rest.

## 2. CCC certificates

The CCC dissolved around 2020–21 and certified phones' certificate chains have
expired; old certified phones no longer pair with Robert's car. We have no CCC key,
and can't get one from the car's side. Passing DAP needs a device key whose
certificate chains to the CCC root — a copied response can't be replayed (fresh nonce
per connection), and extracting a phone's key means defeating its secure element.

**Measure before guessing:** `mlpi probe-phone` ([`probe-phone.md`](probe-phone.md))
drives a real certified phone (Galaxy S6) and saves its DAP certificates with their
validity dates. If they're expired, the wall is expiry (and a car without a clock
may not check it — Part 4 §5). If the car enforces attestation regardless, the only
routes are a 1.0 session (uncertified, parked only), a head-unit engineering mode, or
older firmware. Don't guess before the probe data says so.

## 3. MirrorLink VNC extensions

Implemented from Part 2: display/event configuration, device status, ByeBye, context
information, blocking notifications and touch events are decoded. Not implemented:
content attestation (needs the DAP key), H.264/HSML encodings, server-side scaling.
The first real VNC connection is still recorded byte for byte (`vnc-N-rx.bin`).

## 4. USB VID/PID

Default `0x1d6b:0x0104` (Linux Foundation). The MIB II accepted it in every session.
Other head units may whitelist vendors (Samsung 0x04e8, HTC 0x0bb4, LG 0x1004,
Sony 0x054c) — `[usb] vid/pid` in `mlpi.toml`. Impersonating another vendor is a grey
legal area in some jurisdictions.

## 5. Single USB function

One CDC-NCM function. Some head units might expect a composite device (NCM + ACM).
No evidence for the MIB II, which enumerates and talks to us.

## 6. VW SAI server (side track)

The car also runs "VW SAI-Server" (Standard Application Interface, API level 2) on
TCP 25010, announced via UDP 28500 beacons. `scripts/probe-sai-v*.sh` found its XML
envelope (`<Req id=".."><Capabilities/></Req>`); `Interface`/`Subscribe` need a `url`
we haven't found. Not needed for MirrorLink; parked.

## 7. No wall clock

The Pi has no RTC and no network in the car, so timestamps in the logs are only
relative (sessions are numbered by boot). Take timestamped photos of the head unit to
line things up.

## 8. Python 3.11 minimum

`tomllib` needs Python ≥ 3.11: Raspberry Pi OS Bookworm (3.11) or Trixie (3.13).
