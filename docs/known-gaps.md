# Known gaps

What the current code does **not** solve, and what we know about it. Each entry should
be driven by evidence from a car session before we spend time on a fix.

## 1. The car launches our app but never connects to VNC (current blocker)

**Evidence (2026-05-02, `launch-realcert.pcap`):** every ~10 s the MIB II runs
DHCP → descriptor → Get/SetClientProfile → GetApplicationList → SUBSCRIBE →
GetApplicationList → LaunchApplication → GetApplicationStatus(0x1) = Foreground →
NOTIFY → GetApplicationStatus(0x00000001) = Foreground → silence. No TCP SYN to 5900 in
any capture. From the second round on it fetches our icon, so it does list us.

**Fixed since:** SetClientProfile/GetClientProfile echoed the profile *double-escaped*
(`&amp;lt;clientProfile…`) — the car got text instead of its profile back.

**Hypotheses, now tested automatically by variant rotation** (`config/variants.toml`):
escaping alone; `X_mirrorLinkVersion` 1.1 / explicit 1.0; dropping claims we can't back
(trust levels, audioInfo, cert URL); upper-case `VNC://`; a system app category.

**If no variant works**, next candidates (need spec access or more captures):
- a real MirrorLink phone's AppList/descriptor for comparison (a capture of any
  certified phone against any head unit would settle most of this);
- DAP (device attestation) / `X_Signature` enforcement for 1.1;
- RTP audio server entries, since the car's ClientProfile announces RTP payloads 98/99.

## 2. CCC certificates

The CCC dissolved around 2020–21 and certified phones' certificate chains have
expired; old certified phones no longer pair with Robert's car. We have no CCC key.
If the car enforces attestation, options are an engineering-mode switch on the head
unit, older firmware, or MITM of its trust store. Don't guess before the data says so.

## 3. MirrorLink VNC extensions

CCC-TS-010 adds VNC extension messages (display/event configuration, context
information, …). Our RFB server speaks plain RFB 3.8 with Raw encoding; client
messages of type 128 are *assumed* to be `U8 ext-type, U16 length, payload` and are
logged, not answered. Anything else unknown is dumped raw (`vnc-N-rx.bin`) and the
connection closed. First real VNC connection = the data to implement this properly.

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
