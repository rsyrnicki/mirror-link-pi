# Changelog

## Unreleased

- `update-pi.sh --card /dev/sdX`: update the card in the laptop when the Pi can't be
  reached over USB. Updates are now flushed to the card before the Pi reboots, so a
  power cut right after an update can't leave empty files behind.
- Fixed: a startup race put some sessions on the root file system instead of the data
  partition (the session was created ~2 s before the data partition's file check and
  mount finished). The session now waits for that mount; if the partition is missing,
  the session says so (`DATA-PARTITION-MISSING`).
- If MirrorLink-Pi isn't running 2 minutes after power-on, the Pi writes why to
  `mlpi-boot-problem.txt` on the boot partition (the journal is in RAM, so such a start
  left no trace before). A stuck session setup no longer holds back the USB gadget.
- `collect-logs.sh` also brings home `mlpi-boot-problem.txt` and sessions that landed on
  the root file system (`zips/rootfs-session-NNNN.zip`).
- Faster phone discovery after switching on Wireless debugging: the port scan runs
  every 15 s instead of 30 s and with 2000 parallel connects instead of 400 (it took
  ~20 s in the car); ports that fail twice (the A56 keeps one unrelated port open) are
  skipped for 5 minutes and not taken as "the phone refuses this Pi". Pairing clears
  that list: the phone's real debugging port also fails until the phone is paired.

## 1.1.0 — 2026-10-02

Everyday use in the car: a launcher status bar, pairing on the car screen, Back and
Home buttons, and setup, updates and logs without taking the SD card out. Tested on
the same VW Polo (MIB2 Standard) and Samsung Galaxy A56.

### Phone mode
- Launcher status bar: the phone's clock, signal bars + network type (5G too), battery
  (with charging bolt), media keys (previous, play/pause, next), Do Not Disturb toggle
  and a toggle for the phone's own screen. Polled from the phone every 30 s with one adb
  call.
- Back and Home buttons on the phone video (`back_button`, `home_button`): the MIB2
  Standard sends no knob or hardware keys to MirrorLink at all.
- Pairing from the car screen: open "Pair device with pairing code" on the phone, type
  the code on the car's number pad (port found by mDNS, or typed). Shown automatically
  when the phone is on the Pi's Wi-Fi but doesn't accept the Pi, with a message saying
  which of the two it is (not found / not paired).
- Warning line on the launcher when the phone uses the Pi's Wi-Fi (no internet) as its
  default network, with the fix in `docs/phone-mode.md` (Samsung's "Internet may not be
  available" prompt). Checked every 2 minutes; "avoid bad Wi-Fi" is re-applied then too.
- The car's on-screen keyboard types into the phone (and fills in the pairing code).
  Not yet tried in the car.
- The car's rotary knob: a highlight on the launcher, scroll wheel in apps, for head
  units that send knob events (the MIB2 Standard doesn't).
- The phone is found with its screen off (Android ignores mDNS then): remembered adb
  port, and a port scan when mDNS stays silent. Only phones currently on the Wi-Fi are
  looked for; adb connections stuck "offline" are dropped.
- Experimental, not yet tried: Bluetooth audio auto-connect (variant
  `s6-audio-home-bt`, `[experiment] start_variant` for a trial with automatic fallback).

### Setup and maintenance
- `scripts/install-sd.sh /dev/sdX`: one command downloads Raspberry Pi OS Lite
  (checksum-verified), writes it and sets up everything. The login (`mlpi`/`mlpi`,
  hostname `mlpi`) is created offline, so first boot never waits at the user wizard.
- Over the USB cable, card stays in the Pi: updates (`scripts/update-pi.sh`) and logs
  (`scripts/collect-logs.sh --pi`).
- Power-cut protection: recordings and state on their own partition, system journal in
  RAM.
- Compatibility list (`docs/compatibility.md`), a GitHub issue template for reports, and
  `docs/how-it-works.md`.

### Fixed
- Media keys reached no app (they went to the virtual display); now sent to the app
  that is playing.
- The status bar could stop updating (a poll part without output discarded the whole
  result).
- The pairing page vanished mid-typing when the phone connected anyway; it now says that
  no pairing was needed.

## 1.0.0 — 2026-10-01

First release that works end to end in a car: a VW Polo's MIB2 Standard head unit
(`VW-Mibstd2`) shows a Samsung Galaxy A56's apps (Google Maps, Spotify, …) through a
Raspberry Pi Zero 2 W, with touch and a smooth picture.

**MirrorLink server (the Pi as the "phone")**
- USB CDC-NCM gadget, own DHCP server, SSDP, UPnP/SOAP (TmApplicationServer,
  TmClientProfile, eventing), MirrorLink VNC with context information, DAP stub.
- Protocol variants rotated per connection attempt until the car connects; the winner
  for the VW MIB2 is `s6-audio-home` (app list shaped like a Galaxy S6's).
- Status screen with touch echo, LED progress codes, per-boot session recording
  (events, journal, filtered and size-capped pcap), automatic pruning of old sessions.

**Phone mode**
- Pi Wi-Fi hotspot; the phone connects over Wireless debugging (adb), paired once from
  the laptop with `mlpi pair-phone`.
- scrcpy 4.1 server on an 800×480 virtual display, H.264 decoded on the Pi with
  libavcodec/libswscale via ctypes (portrait apps pillarboxed directly by libswscale).
- The Pi's own launcher: big tiles for favourite apps, all-apps pages, Home button
  (position configurable with `home_button`).
- Video kept live: frames that fall more than `max_lag` behind are dropped and a fresh
  keyframe is requested.
- Internet stays on mobile data: the hotspot offers no gateway, and the Pi sets
  Android's "avoid bad Wi-Fi" (`avoid_bad_wifi`).
- Wi-Fi power saving off on the hotspot (it caused multi-second stalls).

**Tools (laptop)**
- `prepare-sd.sh [--phone]` installs everything onto a stock Raspberry Pi OS Lite card;
  `collect-logs.sh` copies the sessions back and builds upload-sized zips.
- `mlpi report`: per-session summary incl. phone video rate and delay, Wi-Fi link,
  undervoltage/throttling, temperature, and whether the session ended cleanly.
- `mlpi simulate-car` (recorded VW handshake), `mlpi car-view` (live window that talks
  to the Pi exactly like the car), `mlpi phone-preview`, `mlpi probe-phone`.
