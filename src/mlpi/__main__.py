"""CLI dispatcher: ``python3 -m mlpi <subcommand>``.

On the Pi (started by systemd, see systemd/):
  session-init   create this boot's session directory        (mlpi-session.service)
  gadget-up      configure the USB gadget + usb0 address      (mlpi-gadget.service)
  capture        record usb0 into <session>/usb0.pcap         (mlpi-capture.service)
  run            the MirrorLink server itself                 (mlpi.service)

On the laptop:
  simulate-car   play the recorded VW head-unit handshake against a server, then
                 connect to its VNC server and save a screenshot
  report         summarise a session directory brought back from the car
  screenshot     render the status screen to a PNG without any network
  discover       send M-SEARCH and dump replies (debugging)
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

    p_rep = sub.add_parser("report", help="summarise a session directory")
    p_rep.add_argument("session_dir", nargs="+")

    p_shot = sub.add_parser("screenshot", help="render the status screen to PNG")
    p_shot.add_argument("output", nargs="?", default="screen.png")

    p_disc = sub.add_parser("discover", help="send M-SEARCH and print discovered hosts")
    p_disc.add_argument("--interface", "-i", help="bind to this interface (overrides config)")
    p_disc.add_argument("--timeout", "-t", type=float, default=4.0)
    p_disc.add_argument("--verbose", "-v", action="store_true")

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
        import json

        from . import gadget, session
        facts = gadget.gadget_up(cfg.usb)
        directory = session.current_session_dir()
        if directory is not None and directory.is_dir():
            (directory / "gadget.json").write_text(json.dumps(facts, indent=2) + "\n")
        if facts["ifname"] != cfg.network.interface:
            logging.warning("gadget interface is %s but network.interface is %s",
                            facts["ifname"], cfg.network.interface)
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

    if args.cmd == "report":
        from .tools import report
        return report.run([Path(p) for p in args.session_dir])

    if args.cmd == "screenshot":
        from .canvas import Canvas
        from .screen import StatusScreen
        canvas = Canvas(cfg.vnc.width, cfg.vnc.height)
        screen = StatusScreen(canvas, variant_name=lambda: "baseline")
        screen.refresh()
        Path(args.output).write_bytes(canvas.to_png())
        print(args.output)
        return 0

    if args.cmd == "discover":
        from .tools import discover
        if args.interface:
            cfg.network.interface = args.interface
        return discover.run(cfg, timeout=args.timeout, verbose=args.verbose)

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
