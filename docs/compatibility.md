# Compatibility: tested setups

What MirrorLink-Pi has been tried with, and how it went. If you try it with another car
or phone, please add your result: it tells the next person what to expect, and failed
attempts are just as useful as working ones.

**Add an entry:** open an issue with the *Compatibility report* template, or send a pull
request that adds a row below. Please don't post anything that identifies your car or
yourself (VIN, number plate, serial numbers, home Wi-Fi names); `mlpi report` output can
contain such details, so check it before attaching it.

## Head units

| Car / head unit | Head unit model (from `mlpi report`, *Car client profile*) | Pi / OS | MirrorLink-Pi version | Variant that worked | Result | Date | Notes |
|---|---|---|---|---|---|---|---|
| VW Polo, MIB2 Standard | `VW-Mibstd2` | Pi Zero 2 W, Pi OS Lite 64-bit (Trixie) | 1.0.0 | `s6-audio-home` | ✅ works: status screen and phone mode, with touch | 2026-09 | The picture is blocked while driving (stock behaviour for uncertified MirrorLink content). Audio doesn't switch to the phone's Bluetooth by itself while the MirrorLink app runs: select Bluetooth media by hand. The knob, the hardware keys and the overlay's back button send nothing to MirrorLink (the car announces knob shift x/y only, no device keys); its on-screen keyboard works. |

Result legend: ✅ works · 🟡 partly (say what's missing) · ❌ doesn't connect (say how far
it got: the LED stage or the *FURTHEST STAGE* line of `mlpi report`).

## Phones (phone mode)

| Phone | Android / UI version | MirrorLink-Pi version | Result | Date | Notes |
|---|---|---|---|---|---|
| Samsung Galaxy A56 | Android 16, One UI | 1.0.0 | ✅ works | 2026-09 | 20 fps at `dpi = 160`. Samsung's own launcher can't be used on the virtual display (the Pi's launcher replaces it). The phone sometimes resets "avoid bad Wi-Fi"; the Pi sets it again on every connection. |

## What to report

- **Car / head unit:** make, model, year, head unit name if you know it (e.g. MIB2
  Standard, MIB2 High, Discover Media), and the model string from `mlpi report`.
- **Result:** how far it got, which variant worked (`WINNING VARIANT` in the report).
- **Phone mode:** phone model, Android version, which apps you tried, the frame rate and
  delay lines from the report's *Phone:* section.
- **Anything you had to change** in `mlpi.toml`.
