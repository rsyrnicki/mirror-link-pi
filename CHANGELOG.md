# Changelog

## Unreleased

- Launcher status bar: the phone's clock, signal bars + network type, battery (with
  charging bolt), media keys (previous, play/pause, next), Do Not Disturb toggle and a
  toggle for the phone's own screen. Polled from the phone every 30 s with one adb call.

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
