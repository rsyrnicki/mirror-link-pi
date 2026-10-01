from __future__ import annotations

import struct
import types

from mlpi import phone as ph
from mlpi.canvas import Canvas
from mlpi.launcher import (
    DEFAULT_APPS,
    App,
    Launcher,
    display_name,
    parse_app_list,
    parse_apps,
    wrap,
)
from mlpi.video import DisplaySwitch, InputRouter, VideoFrame

LIST_APPS = """[server] INFO: Device: [samsung] samsung SM-A566B (Android 16)
[server] INFO: List of apps:
 * Phone                          com.samsung.android.dialer
 - Spotify                        com.spotify.music
 - Deutschlandticket Wallet-App für Pendler
                                  de.example.ticket
 - Übersetzer                     com.example.translate
"""


def test_parse_scrcpy_app_list_including_wrapped_names():
    apps = parse_app_list(LIST_APPS)
    assert [(a.name, a.package) for a in apps] == [
        ("Deutschlandticket Wallet-App für Pendler", "de.example.ticket"),
        ("Phone", "com.samsung.android.dialer"),
        ("Spotify", "com.spotify.music"),
        ("Übersetzer", "com.example.translate"),
    ]


def test_display_name_and_wrap():
    assert display_name("Übersetzer & Café") == "UEBERSETZER + CAFE"
    assert display_name("🚗 Car") == "CAR"
    assert wrap("HOME ASSISTANT", 9) == ["HOME", "ASSISTANT"]
    assert wrap("A VERY LONG APP NAME HERE", 9) == ["A VERY", "LONG APP."]


def test_config_apps_override_and_defaults():
    assert parse_apps(None) == list(DEFAULT_APPS)
    assert [a.name for a in DEFAULT_APPS] == ["Google Maps", "HERE WeGo", "Spotify", "Audible",
                                            "Home Assistant", "WhatsApp", "Phone"]
    custom = parse_apps([{"name": "Waze", "package": "com.waze", "color": "#33ccff"}])
    assert custom == [App("Waze", "com.waze", "#33ccff")]


def test_home_page_has_favourites_plus_all_apps_and_pages_work():
    launcher = Launcher(VideoFrame(800, 480), list(DEFAULT_APPS))
    kinds = [t for _box, t in launcher.targets if t[0] in ("app", "all")]
    assert kinds[:7] == [("app", a) for a in DEFAULT_APPS] and kinds[7] == ("all", None)
    bar = [t for _box, t in launcher.targets if t[0] not in ("app", "all")]
    assert bar == [("media", "previous"), ("media", "play_pause"), ("media", "next"),
                   ("dnd", None), ("screen", None)]
    launcher.set_all_apps([App(f"App {i}", f"p.{i}") for i in range(20)])
    assert launcher.pages == 2
    launcher.go(0)
    assert ("next", None) in [t for _b, t in launcher.targets]
    assert ("prev", None) not in [t for _b, t in launcher.targets]
    launcher.go(5)                                  # clamped to the last page
    assert launcher.page == 1 and ("prev", None) in [t for _b, t in launcher.targets]


def _center(box):
    x, y, w, h = box
    return x + w // 2, y + h // 2


def test_tap_flow_tile_opens_app_home_button_returns():
    canvas = Canvas(800, 480)
    switch = DisplaySwitch(canvas)
    cfg = types.SimpleNamespace(adb="adb", adb_home="", launcher=True,
                                apps=[{"name": "Spotify", "package": "com.spotify.music"}])
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch)
    sent = []
    link._send = sent.append
    router = InputRouter(types.SimpleNamespace(on_pointer=lambda *a: None,
                                               on_key=lambda *a: None), switch)
    router.attach_phone(link.frame, link)

    link.show_launcher()
    assert switch.showing(link.launcher.frame)
    tile = next(box for box, t in link.launcher.targets if t[0] == "app")
    x, y = _center(tile)
    router.on_pointer(x, y, 1)
    router.on_pointer(x, y, 0)                      # release on the same tile
    assert sent == [ph.start_app_message("com.spotify.music")]
    assert switch.showing(link.frame)

    # A tap in the app goes to the phone as touch events …
    router.on_pointer(400, 200, 1)
    router.on_pointer(400, 200, 0)
    assert [m[1] for m in sent[1:]] == [ph.ACTION_DOWN, ph.ACTION_UP]
    # … the Home button (middle of the right edge) does not; it brings the tiles back.
    bx, by, bw, bh = link.launcher.home_rect
    hx, hy = bx + bw // 2, by + bh // 2
    assert hx > 700 and 200 < hy < 280
    assert link.launcher.in_home_button(hx, hy)
    router.on_pointer(hx, hy, 1)
    router.on_pointer(hx, hy, 0)
    assert len(sent) == 3 and switch.showing(link.launcher.frame)

    # "All apps" opens the list page.
    all_box = next(box for box, t in link.launcher.targets if t[0] == "all")
    router.on_pointer(*_center(all_box), 1)
    router.on_pointer(*_center(all_box), 0)
    assert link.launcher.page == 0


def test_home_button_is_painted_on_video_frames():
    launcher = Launcher(VideoFrame(800, 480), list(DEFAULT_APPS))
    black = bytes(800 * 480 * 2)
    painted = launcher.paint_home_button(black)
    bx, by, bw, bh = launcher.home_rect
    px = struct.unpack_from("<H", painted, ((by + bh // 2) * 800 + bx + 2) * 2)[0]
    assert painted != black and px != 0
    ref = bytearray(black)
    launcher._draw_home_button(ref)
    assert painted == bytes(ref)                                   # cached runs = drawing
    assert painted[(100 * 800 + 400) * 2:(100 * 800 + 401) * 2] == b"\0\0"   # rest untouched


def test_favourites_not_installed_are_hidden_once_the_app_list_is_known():
    launcher = Launcher(VideoFrame(800, 480), list(DEFAULT_APPS))
    assert len([t for _b, t in launcher.targets if t[0] == "app"]) == 7   # before: all shown
    launcher.set_all_apps(parse_app_list(LIST_APPS))                    # Spotify + Phone only
    apps = [t[1].name for _b, t in launcher.targets if t[0] == "app"]
    assert apps == ["Spotify", "Phone"]
    assert ("all", None) in [t for _b, t in launcher.targets]


def test_home_button_positions():
    corners = {}
    for pos in ("left", "right", "top-left", "top-right", "bottom-left", "bottom-right"):
        launcher = Launcher(VideoFrame(800, 480), [], home_button=pos)
        x, y, w, h = launcher.home_rect
        assert 0 <= x and x + w <= 800 and 0 <= y and y + h <= 480
        corners[pos] = (x < 400, y < 240 - h // 2, y > 240 - h // 2)
    assert corners["top-right"][:2] == (False, True)
    assert corners["bottom-left"] == (True, False, True)
    off = Launcher(VideoFrame(800, 480), [], home_button="off")
    assert off.home_rect is None and off.home_button_runs() == []
    assert not off.in_home_button(780, 240)


def test_status_bar_buttons_control_the_phone():
    import time
    import types

    import mlpi.phone as ph
    sent, adb_calls = [], []

    class FakeAdb:
        def run(self, *args, serial="", timeout=20.0):
            adb_calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", launcher=True, apps=[],
                                home_button="right")
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch, adb=FakeAdb())
    link._send = sent.append
    link.show_launcher()

    def tap(target):
        box = next(b for b, t in link.launcher.targets if t == target)
        link.on_pointer(*_center(box), 1)
        link.on_pointer(*_center(box), 0)

    def wait_for(n):
        for _ in range(100):
            if len(adb_calls) >= n:
                return
            time.sleep(0.01)

    tap(("media", "play_pause"))                  # to the playing app, not the display
    wait_for(1)
    assert adb_calls == [("shell", "cmd", "media_session", "dispatch", "play-pause")]
    assert sent == []
    adb_calls.clear()
    tap(("screen", None))
    assert sent == [ph.display_power_message(True)] and link.launcher.state.screen_on
    tap(("dnd", None))
    assert link.launcher.state.dnd is True
    wait_for(1)
    assert adb_calls == [("shell", "cmd", "notification", "set_dnd", "on")]


def test_media_key_falls_back_to_a_key_event():
    import types

    import mlpi.phone as ph
    calls = []

    class FakeAdb:
        def run(self, *args, serial="", timeout=20.0):
            calls.append(args)
            failed = args[1] == "cmd"
            return types.SimpleNamespace(returncode=1 if failed else 0, stdout="",
                                         stderr="Error: unknown command" if failed else "")

    cfg = types.SimpleNamespace(adb="adb", adb_home="", launcher=False)
    link = ph.PhoneLink(cfg, types.SimpleNamespace(width=800, height=480), switch=None,
                        adb=FakeAdb())
    link._media_key("next")
    assert calls[1] == ("shell", "input", "-d", "0", "keyevent", "87")


def test_knob_moves_the_highlight_and_push_opens():
    import types

    import mlpi.phone as ph
    sent = []
    switch = DisplaySwitch(Canvas(800, 480))
    cfg = types.SimpleNamespace(adb="adb", adb_home="", launcher=True, apps=[],
                                home_button="right")
    link = ph.PhoneLink(cfg, switch.new_video_frame(), switch)
    link._send = sent.append
    link.show_launcher()
    assert link.launcher.focus is None
    for keysym in (ph.KNOB_CW, ph.KNOB_CW):          # two clicks clockwise
        link.on_key(keysym, True)
        link.on_key(keysym, False)
    assert link.launcher.focused()[0] == "app"
    second_tile = link.launcher.focused()[1]
    link.on_key(ph.KNOB_CCW, True)
    link.on_key(ph.KNOB_CW, True)
    assert link.launcher.focused()[1] == second_tile
    link.on_key(ph.KNOB_PUSH, True)
    assert sent == []                                # acts on release
    link.on_key(ph.KNOB_PUSH, False)
    assert sent == [ph.start_app_message(second_tile.package)]
    assert switch.showing(link.frame)

    # Inside the app the knob is a scroll wheel at the middle of the picture.
    sent.clear()
    link.on_key(ph.KNOB_CW, True)
    link.on_key(ph.KNOB_CW, False)
    assert sent == [ph.scroll_message(400, 240, 800, 480, 0.0, -1.0)]


def test_scroll_message_matches_scrcpy_layout():
    import mlpi.phone as ph
    # scrcpy test_serialize_inject_scroll_event: (260, 1026) in 1080x1920, h=1, v=-1, buttons=1
    assert ph.scroll_message(260, 1026, 1080, 1920, 1.0, -1.0, 1) == bytes([
        3, 0, 0, 1, 4, 0, 0, 4, 2, 4, 0x38, 7, 0x80, 0x08, 0, 0xF8, 0, 0, 0, 0, 1])
