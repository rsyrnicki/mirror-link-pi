# Field test: one trip to the car

Goal of a trip: either something from the Pi appears on the head unit, or we come back
with enough data to know what to change. Nothing needs to be typed in the car.

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
4. Wait. The Pi boots in ~20–30 s. The car then retries the connection by itself every
   ~10 s; each retry uses the next variant from `config/variants.toml`. One full
   round of the 8 variants takes about 1.5 minutes. **Stay at least 5 minutes.**
5. If the head unit shows a MirrorLink menu or app list, tap our entry
   ("MirrorLink Pi Display") and note what happens.
6. Read the LED (it shows the best result of this boot):

| LED | Meaning |
|---|---|
| short blink every 3 s | Pi running, car has not enumerated the USB gadget |
| 1 blink, pause | USB link up |
| 2 blinks, pause | the car took an address (DHCP) |
| 3 blinks, pause | the car fetched our UPnP descriptor |
| 4 blinks, pause | the car launched our app — **this is where it stopped so far** |
| solid on | **the car connected to our VNC server** |

7. If you see the status screen ("MIRRORLINK-PI", boot number, moving green block,
   colour bars) on the head unit: take photos, touch the screen and turn the knob a few
   times (should show up in `TOUCH:` / `KEY:`), and stay a few minutes.
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
| `sessions/NNNN/summary.txt` | furthest stage, winning variant, notes |

If a variant won, `winner-variant` holds its name and the next boot starts with it.
To keep using it without rotation, set `[experiment] mode = "fixed"` and
`fixed_variant = "<name>"` in `mlpi.toml` on the boot partition.
