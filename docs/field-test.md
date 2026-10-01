# Field test: one trip to the car

Goal of a trip: the Pi's screen (or, in phone mode, your phone's apps) on the head unit,
and if anything goes wrong, enough data in the logs to know what to change. Nothing
needs to be typed in the car.

## Pre-flight at home

Do this once after every `prepare-sd.sh`. It exercises the complete chain the car will
use — USB gadget, DHCP, UPnP/SOAP, eventing and VNC — with the laptop playing the car.

1. Boot the Pi with the card (first boot: wait for the resize reboot).
2. Connect the Pi's **USB** port (not PWR) to the laptop with a *data* cable.
3. Watch the green LED: 1 blink (USB link) → 2 blinks (the laptop took an address).
   The laptop now has a new network interface with `192.168.7.44`.
4. Run the simulator from the repo:
   ```bash
   PYTHONPATH=src python3 -m mlpi simulate-car --target 192.168.7.2
   ```
   It replays the VW head unit's recorded handshake, connects to the VNC server and
   writes `car-view.png`. Expect `OK` at the end and the LED to go **solid**.
5. Optional: open any VNC viewer on `192.168.7.2:5900` and click around — the touches
   appear as orange dots and in the `TOUCH:` line.
6. Unplug. (If the laptop lost internet while connected — it routes via the Pi — that
   is expected and goes away when unplugged.)

Pre-flight boots are recorded as sessions too. Simulator traffic is marked
`simulated` and never locks a variant.

## In the car

1. Car on (engine or ignition — the head unit must be running), phone/other USB
   devices unplugged, MirrorLink enabled in the head unit settings if there is such an option.
2. Plug the Pi (USB port) into the car's USB socket.
3. **Note the time** on your phone (photos of the head unit screen with timestamps help
   a lot: they let us line up what the screen showed with the logs).
4. Wait. The Pi boots in ~20–30 s. The car then connects by itself. On a car the Pi
   hasn't seen before, each retry (~every 10 s) uses the next protocol variant from
   `config/variants.toml`, starting with `s6-audio-home`, the one the VW MIB2 accepts.
   If nothing happens, **stay at least 5 minutes** so all variants get a turn.
5. When the head unit shows its MirrorLink app list, tap **MirrorLink Pi**.
6. Read the LED (it shows the best result of this boot):

| LED | Meaning |
|---|---|
| short blink every 3 s | Pi running, car has not enumerated the USB gadget |
| 1 blink, pause | USB link up |
| 2 blinks, pause | the car took an address (DHCP) |
| 3 blinks, pause | the car fetched our UPnP descriptor |
| 4 blinks, pause | the car launched our app |
| solid on | **the car connected to our VNC server** |

7. You should see the status screen ("MIRRORLINK-PI", boot number, moving green block,
   colour bars), or in phone mode the launcher once the phone is connected
   ([`phone-mode.md`](phone-mode.md#in-the-car)). Touches show up in `TOUCH:` / `KEY:`.
8. Unplug when done. Sudden power loss is fine: logs are synced every 2–3 s.

Optional second round: unplug/replug once. That is a new boot, i.e. a new session
directory, and the car's "fresh plug-in" behaviour gets recorded too.

## Back home

```bash
sudo ./scripts/collect-logs.sh /dev/sdX
less car-logs/<timestamp>/REPORT.txt
```

`REPORT.txt` has, per session: the furthest stage, a table of every attempt (variant
and the requests the car made), per-variant success counts, the car's DHCP fingerprint,
every HTTP error we returned and all VNC activity. The raw material is next to it:

| File | Content |
|---|---|
| `sessions/NNNN/events.jsonl` | the timeline: every request/response in full, NOTIFYs, DHCP, VNC messages |
| `sessions/NNNN/usb0.pcap` | every packet on the USB link — open in Wireshark |
| `sessions/NNNN/journal.txt` | the whole boot log incl. kernel USB messages |
| `sessions/NNNN/vnc-N-rx.bin` | raw bytes the car sent on VNC connection N |
| `sessions/NNNN/usb.jsonl` | USB level: MirrorLink USB command (the car's ML version!), UDC states, fallbacks |
| `sessions/NNNN/summary.txt` | furthest stage, winning variant, notes |

If a variant won, `winner-variant` holds its name and the next boot starts with it.
To keep using it without rotation, set `[experiment] mode = "fixed"` and
`fixed_variant = "<name>"` in `mlpi.toml` on the boot partition.
