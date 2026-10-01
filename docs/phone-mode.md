# Phone mode: your Android phone on the car screen

The Pi mirrors an Android phone to the car with [scrcpy](https://github.com/Genymobile/scrcpy):
the phone runs Google Maps, Spotify and so on in a separate **800×480 virtual display**
(the car's screen size). The phone streams that display to the Pi, and the Pi shows it on
the car through the MirrorLink connection that already works. Touches on the car screen go
back to the phone.

```
Phone ──Wi-Fi (Pi hotspot)── H.264 video / touch ── Pi ──USB MirrorLink VNC── car
```

- **Internet** stays on the phone's mobile data. The Pi's hotspot offers no internet, so
  the phone keeps using mobile data for Maps and Spotify.
- **Audio** is not part of this. Pair the phone with the car over Bluetooth as usual; music
  and navigation voice go that way.
- **While driving** the car blanks uncertified MirrorLink content (see the README). That
  needs the head unit's own setting.

**Tested** with a Samsung Galaxy A56 (Android 16, One UI) and a VW Polo's MIB2 Standard
head unit: Google Maps, HERE WeGo, Spotify, Audible and Home Assistant, at 20 fps with
the Pi mostly idle. Other Android phones (Android 11 or newer, for Wireless debugging)
should work the same way, but haven't been tried.

## One-time setup

### On the laptop

```bash
sudo apt install adb qemu-user-static python3-tk ffmpeg   # ffmpeg only for phone-preview
# Fedora: sudo dnf install android-tools qemu-user-static python3-tkinter ffmpeg
git pull
```

### On the phone (Samsung A56, Android 16)

1. **Developer options:** Settings → About phone → Software information → tap
   *Build number* 7 times.
2. Settings → Developer options → **Wireless debugging: on**.
3. If Samsung's **Auto Blocker** is on (Settings → Security and privacy), it may block
   debugging. Turn it off if pairing or connecting fails.
4. Recommended: add the **Wireless debugging** tile to the quick settings panel (Developer
   options → Quick settings developer tiles), so switching it on takes one tap.

### Pair the phone with the Pi's key

**In the car (easiest):** with the phone on the Pi's Wi-Fi, open *Wireless debugging →
Pair device with pairing code* on the phone. Within a few seconds the car screen shows a
number pad with the phone's pairing port already filled in. Type the 6-digit code from
the phone and tap **PAIR**; the Pi connects right after. If the port isn't found, type
the port shown on the phone too (the number after the colon). The same screen also comes
up by itself when the phone has been on the Pi's Wi-Fi for 20 s without accepting the Pi
(e.g. after the phone forgot the pairing); **LATER** hides it for two minutes.

Tip: Android revokes debugging authorisations that haven't been used for 7 days. Turn
that off in *Developer options → Disable adb authorization timeout*, or the phone may
forget the Pi between drives.

**From the laptop** (alternative, before preparing the card):

The phone only accepts adb connections from keys it has been paired with. The Pi can't
show you a pairing screen, so the laptop pairs **the Pi's key** once:

```bash
PYTHONPATH=src python3 -m mlpi pair-phone
```

Phone and laptop must be on the same Wi-Fi (your home network). On the phone: Wireless
debugging → *Pair device with pairing code*. The command asks for the **IP address:port**
and the **pairing code** shown in that dialog; type them while the dialog is still open.
It ends with "Paired.". Then prepare the SD card (next step), which copies the key.
The key is stored in `~/.config/mlpi/adb/` on the laptop. Keep it private: it allows
controlling your phone while Wireless debugging is on.

### Prepare the SD card with phone mode

```bash
sudo ./scripts/prepare-sd.sh --phone /dev/sdX
```

On top of the normal install, this:

- installs `adb` and FFmpeg's decoder libraries into the image (about 100 MB, via qemu;
  takes about 3 minutes)
- installs the scrcpy server, version 4.1, checksum-pinned (`scripts/fetch-scrcpy-server.sh`)
- copies the paired adb key
- turns phone mode on in `mlpi.toml` on the boot partition, with a **random Wi-Fi
  password**, which is printed at the end

Running it again later keeps the same password.

## Desk tests (before the car)

**1. Phone → laptop.** This checks the phone side on its own. Plug the phone into the
laptop by USB (USB debugging on), or connect it wirelessly:

```bash
./scripts/fetch-scrcpy-server.sh
PYTHONPATH=src python3 -m mlpi phone-preview --screenshot phone.png
```

Then open `127.0.0.1:5900` in any VNC viewer (Remmina, `vncviewer 127.0.0.1::5900`).
You should see an 800×480 screen with Maps, and mouse clicks act as touches. Try
`--start-app <package>` to open one app directly and `--dpi 160` or `--dpi 240` to find a comfortable
size.

**2. Phone → Pi → laptop.** This is the real chain:

1. Plug the Pi into the laptop as for the normal pre-flight.
2. On the phone, join the Wi-Fi **MirrorLink-Pi** (password from `mlpi.toml`). When Android
   says the network has no internet, just ignore it. Don't choose **stay connected** /
   "don't ask again": that makes the Pi's Wi-Fi (which has no internet) the phone's
   internet connection.
3. Switch Wireless debugging on.
4. Open the car's view in a window, over the USB link:
   ```bash
   sudo apt install python3-tk        # once
   PYTHONPATH=src python3 -m mlpi car-view
   ```
   It talks to the Pi exactly like the head unit (MirrorLink handshake, RGB565, one
   update request at a time); clicks and drags are touches. Every 5 s it prints
   updates/s, MB/s and "tap → screen" (press to next picture). Desktop VNC viewers
   (Remmina, TigerVNC) ask for other colour formats that the Pi has to convert
   frame by frame in Python, and they pace updates their own way, so they look much
   laggier than the car ever gets.
5. The Pi's status screen line `PHONE:` shows progress. Once it says `streaming …`, run
   `simulate-car`: its screenshot should show the phone's display.

## In the car

1. Turn the car on and plug the Pi's **USB** port into the car's USB socket. After about
   30 s the head unit lists **MirrorLink Pi** among the MirrorLink apps; open it. The car
   shows the Pi's status screen.
2. The first time, join the Wi-Fi **MirrorLink-Pi** on the phone (password from
   `mlpi.toml`; ignore the "no internet" prompt). After that the phone joins by itself.
3. Switch Wireless debugging on (quick settings tile) if it's off.
4. Within a few seconds the car shows the Pi's launcher; tap an app. With the phone
   disconnected, the car shows the Pi's status screen again; the Pi reconnects by
   itself when the phone is back.

The car's back key acts as Android *Back*, and Home and OK are mapped too. Text typed
on the car's keyboard goes into the focused text field on the phone; right after
typing, the keyboard's delete key deletes a character and Enter submits (otherwise
they act as Back and OK). On the pairing page the keyboard's digits fill in the code.
Every key the car sends is logged (`vnc_key`; unhandled ones also `phone_key_unmapped`)
so more keys can be mapped.

## The launcher

Android puts only a special "secondary home" on scrcpy's virtual display — on Samsung
One UI's stripped-down DeX launcher with tiny icons; normal launchers (One UI, Nothing, …)
can't be used there. So the Pi draws its own home screen for the car:

- **Home page:** large tiles for up to 7 favourite apps (`apps` in `mlpi.toml`) plus
  **All apps**. Favourites that aren't installed on the phone are hidden.
- **Status bar** at the top of the home page, from the phone (updated every 30 s):
  the phone's **clock**, **signal** bars with the network type (5G/4G/3G), **battery**
  level (a bolt while charging), and buttons for **previous / play-pause / next**
  (sent to whatever app is playing), **Do Not Disturb** (purple when on) and the
  **phone's own screen** (blue when on; it is switched off while mirroring to save
  battery, tap to light it up, e.g. to see a notification).
- **All apps:** every launchable app on the phone, 12 per page, alphabetical. The list
  comes from the phone itself when it connects.
- **Home button:** a small house in the middle of the right edge, over every app, brings
  the tiles back (`home_button` moves it, see the settings).

Tiles show the app's name and initial rather than its real icon (scrcpy has no way to
send icons). Names are drawn with the Pi's pixel font: letters are converted, e.g. Ä → AE.

## What the `PHONE:` line means

| Status | Meaning / fix |
|---|---|
| `waiting for the phone on Wi-Fi` | The phone isn't on MirrorLink-Pi. |
| `phone on Wi-Fi, but wireless debugging is off` | Switch Wireless debugging on. (With the phone's screen off, Android ignores the usual network announcement, so the Pi also scans for the debugging port, at most every 30 s, and remembers it for next time.) |
| `phone refused adb: pair it` | Pair on the car screen (see *Pair the phone with the Pi's key*). |
| `phone doesn't know this Pi: pair it on the car screen` | The number pad is up: open *Pair device with pairing code* on the phone and type the code. |
| `starting scrcpy on …` | Connected; starting the stream. |
| `streaming … 800x480` | Working. |
| `phone lost: …` | The reason is in the session log. The Pi retries every few seconds. |

Everything is recorded in the session: `phone_*` events in `events.jsonl`, the scrcpy
server's output in `phone-server.log`, the frame rate and the delay behind the phone
every 5 s (`phone_fps`), the phone's Wi-Fi link every 10 s (`wifi`) and the phone's
network state (`phone-connectivity.txt`).

## If it lags, freezes or has no internet

**No internet, and the car shows "No internet: phone uses the Pi's Wi-Fi":** the phone
has been told to stay connected to `MirrorLink-Pi` even though it has no internet, so
it sends all traffic there. "Avoid bad Wi-Fi" doesn't override that choice. To undo it:

1. On the phone: Settings → Connections → Wi-Fi → `MirrorLink-Pi` → ⚙ → **Forget**.
2. Join `MirrorLink-Pi` again with its password.
3. When the phone says the network has no internet, **ignore the message**: don't tap
   it and don't choose "stay connected" / "keep Wi-Fi connection".
4. Optional, so the phone doesn't reset the setting: Settings → Connections → Wi-Fi →
   ⋮ → Intelligent Wi-Fi → **Switch to mobile data** on (Samsung; on other phones
   Developer options → "Mobile data always active" plus "Switch to mobile data").

Collect the logs (`./scripts/collect-logs.sh --pi`, or `sudo ./scripts/collect-logs.sh
/dev/sdX` with the card in the laptop) and look at the **Phone:**
and **Pi health:** sections of the session's `REPORT.txt`:

| Report line | Healthy | If not |
|---|---|---|
| `decoded fps` | close to `max_fps` | lower `max_fps` / `bit_rate` |
| `delay behind the phone per 5 s` | median under ~0.7 s | high with high Pi load: lower `max_fps`; with low load: Wi-Fi (next line) |
| `phone Wi-Fi link` | signal better than about −65 dBm, few failures | move the Pi/phone, or try another `wifi_channel` (1, 6 or 11) |
| `phone network state` → `Active default network` | the mobile network (not WIFI) | don't choose "stay connected" on the no-internet prompt; keep `avoid_bad_wifi = true` |
| `power/thermal flags seen` | `none` | the car's USB port can't supply enough: use a better cable or a 2.4 A socket |

A desktop VNC viewer (Remmina, TigerVNC) is no good for judging speed: it asks for a
colour format the Pi has to convert in Python. Use `mlpi car-view` (desk test 2).

## Settings (`[phone]` in mlpi.toml)

| Key | Default | |
|---|---|---|
| `launcher` | `true` | the Pi's own launcher (below) |
| `apps` | Google Maps, HERE WeGo, Spotify, Audible, Home Assistant, WhatsApp, Phone | home-page tiles, max 7: `apps = [{name = "Waze", package = "com.waze", colour = "#33ccff"}]` |
| `start_app` | `""` | open this app directly instead of the launcher |
| `dpi` | `120` | density. 120 makes the display count as a tablet, so apps use landscape layouts; higher = larger UI (160 reads well in the car) but more portrait-only apps get side bars |
| `max_fps` / `bit_rate` | `20` / `3000000` | what the car tests ran with; lower them if the Zero 2 W can't keep up (see `phone_fps`) |
| `decoder` | `""` (software) | `h264_v4l2m2m` tries the Pi's hardware decoder (experimental) |
| `max_lag` | `2.0` | seconds the picture may fall behind the phone before the Pi drops the backlog and asks for a fresh keyframe (0 = never) |
| `avoid_bad_wifi` | `true` | sets Android's "avoid bad Wi-Fi" (`network_avoid_bad_wifi=1`) on the phone so mobile data stays its internet while it is on the Pi's Wi-Fi; undo with `adb shell settings delete global network_avoid_bad_wifi` |
| `bt_address` | `""` | the phone's Bluetooth address for `s6-audio-home-bt` (`""` = read it from the phone) |
| `knob_invert` | `false` | the car's knob scrolls the other way inside apps |
| `home_button` | `"right"` | where the Pi's Home button sits on the phone video: `right` / `left` (middle of that edge), `top-left`, `top-right`, `bottom-left`, `bottom-right`, or `off` |
| `back_button` | `true` | a Back button above the Home button (head units like the MIB2 Standard send no Back key to MirrorLink) |
| `system_decorations` | `false` | Samsung's secondary-display launcher + navigation bar on the virtual display |
| `keep_active` | `true` | keeps the phone awake while mirroring |
| `screen_off` | `true` | turns the phone's own screen off (without locking — a locked phone blanks the car screen) |
| `wifi_ssid` / `wifi_password` / `wifi_country` / `wifi_channel` | | the Pi's hotspot |

## Bluetooth audio auto-connect (experimental)

Normally the car doesn't switch to the phone's Bluetooth by itself while the MirrorLink
app runs. The variant `s6-audio-home-bt` additionally tells the car that the "MirrorLink
phone" has Bluetooth audio (A2DP for music, HFP for calls) at the phone's Bluetooth
address, which a MirrorLink head unit may use to select the phone's Bluetooth as the
audio source on its own. To try it, set in `mlpi.toml`:

```toml
[experiment]
mode = "rotate"
start_variant = "s6-audio-home-bt"
```

`rotate` matters: if the car doesn't connect with the new variant, the Pi falls back to
`s6-audio-home` after two attempts (about 6 s), so the screen still comes up.

The Pi reads the phone's Bluetooth address over adb the first time the phone connects
and remembers it, so the entries appear **from the next boot on**. To skip that, set
`bt_address = "AA:BB:CC:DD:EE:FF"` under `[phone]` (Settings → About phone → Status
information → Bluetooth address). In the report, *the car launched the phone's
Bluetooth BTA2DP* means the car took the hint.

## Good to know

- **Samsung's own launcher can't be used** on the virtual display (Android only allows a
  stripped-down "secondary home" there), hence the Pi's launcher.
- **Don't lock the phone** while mirroring: a locked phone blanks the car screen. The Pi
  turns the phone's own screen off without locking it (`screen_off`).
- **Wireless debugging** may switch itself off when the phone leaves a Wi-Fi network.
  The quick settings tile makes switching it back on one tap.
- **Portrait-only apps** (e.g. some audiobook apps) show with black bars left and right.
- The phone sometimes resets Android's "avoid bad Wi-Fi" setting; the Pi sets it again
  on every connection and every 2 minutes.
- **No knob, no hardware keys** on the VW MIB2 Standard: it tells the Pi it only has
  "knob shift x/y" and no device keys, and in practice sends neither (nor its overlay's
  back button). Hence the Pi's own **Back** button above Home. The car's keyboard
  does work: it sends the whole text when you confirm it.

## Licences

scrcpy is Apache-2.0, adb is part of Android's platform tools (Apache-2.0), and FFmpeg's
libraries are LGPL/GPL. All of them are downloaded at install time from their official
sources, and none of them are committed to this repository.
