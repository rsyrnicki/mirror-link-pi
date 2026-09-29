"""CLI dispatcher: ``python3 -m mlpi <subcommand>``.

On the Pi (started by systemd, see systemd/):
  session-init   create this boot's session directory        (mlpi-session.service)
  gadget-up      USB gadget + usb0 address, then stay running to answer the
                 MirrorLink USB command and log USB events  (mlpi-gadget.service)
  capture        record usb0 into <session>/usb0.pcap         (mlpi-capture.service)
  run            the MirrorLink server itself                 (mlpi.service)

On the laptop:
  probe-phone    drive a real MirrorLink phone as a reference implementation, saving
                 its descriptor, app list, DAP certificates and screen (Linux, root)
  simulate-car   play the recorded VW head-unit handshake against a server, then
                 connect to its VNC server and save a screenshot
  report         summarise a session directory brought back from the car
  screenshot     render the status screen to a PNG without any network
  discover       send M-SEARCH and dump replies (debugging)
  pair-phone     pair an Android phone for phone mode (Wireless debugging), with the
                 adb key that prepare-sd.sh --phone puts on the SD card
  phone-preview  mirror the phone into a local VNC server, to try phone mode at the
                 desk with any VNC viewer (docs/phone-mode.md)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config as config_mod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mlpi", description="MirrorLink-Pi")
    parser.add_argument("--config", "-c", help="path to mlpi.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="run the MirrorLink server (Pi)")
    sub.add_parser("session-init", help="create this boot's session directory (Pi)")
    sub.add_parser("gadget-up", help="configure the USB gadget and usb0 (Pi, root)")
    sub.add_parser("capture", help="record usb0 into the session pcap (Pi, root)")

    p_sim = sub.add_parser("simulate-car", help="replay the VW head-unit handshake")
    p_sim.add_argument("--target", required=True, help="IP of the MirrorLink server")
    p_sim.add_argument("--http-port", type=int, default=8080)
    p_sim.add_argument("--callback-ip", default="",
                       help="our IP as seen by the server (default: auto)")
    p_sim.add_argument("--no-vnc", action="store_true", help="stop after LaunchApplication")
    p_sim.add_argument("--screenshot", default="car-view.png",
                       help="where to save the VNC frame (default: car-view.png)")
    p_sim.add_argument("--attempts", type=int, default=1,
                       help="repeat the handshake N times (exercises variant rotation)")

    p_probe = sub.add_parser("probe-phone",
                             help="probe a real MirrorLink phone as a reference (Linux, root)")
    p_probe.add_argument("--list", action="store_true", help="list attached USB devices and exit")
    p_probe.add_argument("--device", default="", help="target a device by BUS:ADDR")
    p_probe.add_argument("--vendor", default="", help="target a USB vendor id, e.g. 0x04e8")
    p_probe.add_argument("--version", default="1.1", choices=["1.0", "1.1", "1.2", "1.3"],
                         help="MirrorLink version to announce in the USB command")
    p_probe.add_argument("--interface", default="",
                         help="skip the USB command; use this already-up interface")
    p_probe.add_argument("--session-root", default="", help="where to record (default: config)")

    p_rep = sub.add_parser("report", help="summarise a session directory")
    p_rep.add_argument("session_dir", nargs="+")

    p_shot = sub.add_parser("screenshot", help="render the status screen to PNG")
    p_shot.add_argument("output", nargs="?", default="screen.png")

    p_disc = sub.add_parser("discover", help="send M-SEARCH and print discovered hosts")
    p_disc.add_argument("--interface", "-i", help="bind to this interface (overrides config)")
    p_disc.add_argument("--timeout", "-t", type=float, default=4.0)
    p_disc.add_argument("--verbose", "-v", action="store_true")

    p_pair = sub.add_parser("pair-phone", help="pair the phone for phone mode (laptop)")
    p_pair.add_argument("target", nargs="?", default="", help="IP:PORT from the pairing dialog")
    p_pair.add_argument("code", nargs="?", default="", help="pairing code")

    p_prev = sub.add_parser("phone-preview", help="mirror the phone into a local VNC server")
    p_prev.add_argument("--serial", default="", help="adb serial (default: first device)")
    p_prev.add_argument("--port", type=int, default=5900)
    p_prev.add_argument("--screenshot", default="", help="save the first frame as PNG")
    p_prev.add_argument("--server-jar", default="", help="default: vendor/scrcpy-server")
    p_prev.add_argument("--start-app", default=None, help="package to start ('' = launcher)")
    p_prev.add_argument("--dpi", type=int, default=0)
    p_prev.add_argument("--max-fps", type=int, default=0)
    p_prev.add_argument("--bit-rate", type=int, default=0, help="e.g. 8000000")
    p_prev.add_argument("--screen-on", action="store_true",
                        help="keep the phone's own screen on while mirroring")

    args = parser.parse_args(argv)
    cfg = config_mod.load(path=args.config)

    if args.cmd == "run":
        from . import runner
        return runner.run(cfg)

    if args.cmd == "session-init":
        from . import session
        directory = session.init_session(Path(cfg.session.root))
        print(directory)
        return 0

    if args.cmd == "gadget-up":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        from . import gadget, session
        directory = session.current_session_dir()
        gadget.run_daemon(cfg.usb, directory if directory and directory.is_dir() else None)
        return 0

    if args.cmd == "capture":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        from . import capture, session
        directory = session.current_session_dir() or Path(cfg.session.root)
        directory.mkdir(parents=True, exist_ok=True)
        capture.capture(cfg.network.interface, directory / f"{cfg.network.interface}.pcap")
        return 0

    if args.cmd == "simulate-car":
        from .tools import simulate_car
        return simulate_car.run(
            target=args.target, http_port=args.http_port, callback_ip=args.callback_ip,
            vnc=not args.no_vnc, screenshot=Path(args.screenshot), attempts=args.attempts)

    if args.cmd == "probe-phone":
        from .tools import probe_phone
        if args.list:
            from . import usbhost
            for d in usbhost.list_devices():
                print(d.describe())
            return 0
        vendor = int(args.vendor, 16) if args.vendor else None
        return probe_phone.run(cfg, device_spec=args.device, vendor=vendor,
                               version=args.version, interface=args.interface,
                               session_root=args.session_root)

    if args.cmd == "report":
        from .tools import report
        return report.run([Path(p) for p in args.session_dir])

    if args.cmd == "screenshot":
        from .canvas import Canvas
        from .screen import StatusScreen
        canvas = Canvas(cfg.vnc.width, cfg.vnc.height)
        screen = StatusScreen(canvas, variant_name=lambda: "spec-1.0")
        screen.refresh()
        Path(args.output).write_bytes(canvas.to_png())
        print(args.output)
        return 0

    if args.cmd == "discover":
        from .tools import discover
        if args.interface:
            cfg.network.interface = args.interface
        return discover.run(cfg, timeout=args.timeout, verbose=args.verbose)

    if args.cmd == "pair-phone":
        from .tools import phone_tools
        return phone_tools.pair_phone(args.target, args.code)

    if args.cmd == "phone-preview":
        from .tools import phone_tools
        return phone_tools.phone_preview(
            serial=args.serial, port=args.port,
            screenshot=Path(args.screenshot) if args.screenshot else None,
            server_jar=args.server_jar, start_app=args.start_app, dpi=args.dpi or None,
            max_fps=args.max_fps or None, bit_rate=args.bit_rate or None,
            screen_off=not args.screen_on,
            width=cfg.vnc.width, height=cfg.vnc.height)

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
