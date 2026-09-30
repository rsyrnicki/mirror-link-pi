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

Tested so far without the real phone and car: the scrcpy 4.1 protocol against its own
test vectors, and the whole chain from a fake phone streaming real H.264 to the car
simulator. The decoder was also run on Raspberry Pi OS's own ARM64 FFmpeg 7.1 under
emulation. The first run with the real A56 is the next step, and the desk test below is
built for exactly that.

## One-time setup

### On the laptop

```bash
sudo apt install adb qemu-user-static ffmpeg   # ffmpeg only for the desk preview
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

The phone only accepts adb connections from keys it has been paired with. The Pi can't
show you a pairing screen, so the laptop pairs **the Pi's key** once:

```bash
PYTHONPATH=src python3 -m mlpi pair-phone
```

Phone and laptop must be on the same Wi-Fi (your home network). On the phone: Wireless
debugging → *Pair device with pairing code*, then type the address:port and code it shows.
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

1. Plug in the Pi and wait for the MirrorLink Pi app, as before.
2. The phone joins **MirrorLink-Pi** automatically once it has been saved. Switch Wireless
   debugging on (quick settings tile) if it's off. Android may switch it off whenever the
   phone leaves a Wi-Fi network.
3. Within a few seconds the car shows the phone's 800×480 display with Maps. With the
   phone disconnected, the car shows the Pi's status screen again.

The car's back key acts as Android *Back*, and Home and OK are mapped too. Other
knob and key events are logged (`phone_key_unmapped`) so they can be mapped later.

## The launcher

Android puts only a special "secondary home" on scrcpy's virtual display — on Samsung
One UI's stripped-down DeX launcher with tiny icons; normal launchers (One UI, Nothing, …)
can't be used there. So the Pi draws its own home screen for the car:

- **Home page:** large tiles for up to 7 favourite apps (`apps` in `mlpi.toml`) plus
  **All apps**. Favourites that aren't installed on the phone are hidden.
- **All apps:** every launchable app on the phone, 12 per page, alphabetical. The list
  comes from the phone itself when it connects.
- **Home button:** a small house in the bottom-left corner of every app brings the tiles
  back (the car's Home key does too).

Tiles show the app's name and initial rather than its real icon (scrcpy has no way to
send icons). Names are drawn with the Pi's pixel font: letters are converted, e.g. Ä → AE.

## What the `PHONE:` line means

| Status | Meaning / fix |
|---|---|
| `waiting for the phone on Wi-Fi` | The phone isn't on MirrorLink-Pi. |
| `phone on Wi-Fi, but wireless debugging is off` | Switch Wireless debugging on. |
| `phone refused adb: pair it` | Run `mlpi pair-phone` again, then `prepare-sd.sh --phone`. |
| `starting scrcpy on …` | Connected; starting the stream. |
| `streaming … 800x480` | Working. |
| `phone lost: …` | The reason is in the session log. The Pi retries every few seconds. |

Everything is recorded in the session: `phone_*` events in `events.jsonl`, the scrcpy
server's output in `phone-server.log`, and the frame rate every 10 s (`phone_fps`).

## Settings (`[phone]` in mlpi.toml)

| Key | Default | |
|---|---|---|
| `launcher` | `true` | the Pi's own launcher (below) |
| `apps` | Google Maps, HERE WeGo, Spotify, Audible, Home Assistant, WhatsApp, Phone | home-page tiles, max 7: `apps = [{name = "Waze", package = "com.waze", colour = "#33ccff"}]` |
| `start_app` | `""` | open this app directly instead of the launcher |
| `dpi` | `120` | density. 120 makes the display count as a tablet, so apps use landscape layouts; higher = larger UI but portrait-only apps get side bars |
| `max_fps` / `bit_rate` | `30` / `4000000` | lower them if the Zero 2 W can't keep up (see `phone_fps`) |
| `decoder` | `""` (software) | `h264_v4l2m2m` tries the Pi's hardware decoder (experimental) |
| `max_lag` | `2.0` | seconds the picture may fall behind the phone before the Pi drops the backlog and asks for a fresh keyframe (0 = never) |
| `avoid_bad_wifi` | `true` | sets Android's "avoid bad Wi-Fi" (`network_avoid_bad_wifi=1`) on the phone so mobile data stays its internet while it is on the Pi's Wi-Fi; undo with `adb shell settings delete global network_avoid_bad_wifi` |
| `home_button` | `"right"` | where the Pi's Home button sits on the phone video: `right` / `left` (middle of that edge), `top-left`, `top-right`, `bottom-left`, `bottom-right`, or `off` |
| `system_decorations` | `false` | Samsung's secondary-display launcher + navigation bar on the virtual display |
| `keep_active` | `true` | keeps the phone awake while mirroring |
| `screen_off` | `true` | turns the phone's own screen off (without locking — a locked phone blanks the car screen) |
| `wifi_ssid` / `wifi_password` / `wifi_country` / `wifi_channel` | | the Pi's hotspot |

## Open questions (to check with the real A56)

- Whether One UI shows its launcher on scrcpy's virtual display. If not, set `start_app`
  (Maps is the default).
- Whether the virtual display keeps rendering while the phone's own screen is locked.
- Whether Android 16 switches Wireless debugging off after every Wi-Fi change.
- Frame rate on the Zero 2 W: software H.264 decoding is expected to manage roughly
  20–30 fps at 800×480, and the session logs will show the real number.

## Licences

scrcpy is Apache-2.0, adb is part of Android's platform tools (Apache-2.0), and FFmpeg's
libraries are LGPL/GPL. All of them are downloaded at install time from their official
sources, and none of them are committed to this repository.
