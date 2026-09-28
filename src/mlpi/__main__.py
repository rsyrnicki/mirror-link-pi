"""CLI dispatcher: ``python -m mlpi <subcommand>`` or the ``mlpi`` console script.

Subcommands:
  run            start SSDP responder + HTTP descriptor server (production)
  discover       send M-SEARCH and dump replies (debugging)
  simulate-car   pretend to be a head unit (replay captured M-SEARCH at the responder)
"""

from __future__ import annotations

import argparse
import sys

from . import config as config_mod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mlpi", description="MirrorLink-Pi")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run SSDP responder + HTTP descriptor server")
    p_run.add_argument("--config", "-c", help="path to mlpi.toml")

    p_disc = sub.add_parser("discover", help="send M-SEARCH and print discovered hosts")
    p_disc.add_argument("--interface", "-i", help="bind to this interface (overrides config)")
    p_disc.add_argument("--timeout", "-t", type=float, default=4.0)
    p_disc.add_argument("--verbose", "-v", action="store_true")
    p_disc.add_argument("--config", "-c", help="path to mlpi.toml")

    p_sim = sub.add_parser("simulate-car", help="send a fake M-SEARCH at a target host")
    p_sim.add_argument("--target", required=True, help="IP of the device under test")
    p_sim.add_argument("--port", type=int, default=1900)
    p_sim.add_argument("--config", "-c", help="path to mlpi.toml")

    args = parser.parse_args(argv)
    cfg = config_mod.load(path=args.config)

    if args.cmd == "run":
        from . import runner
        return runner.run(cfg)
    if args.cmd == "discover":
        from .tools import discover
        if args.interface:
            cfg.network.interface = args.interface
        return discover.run(cfg, timeout=args.timeout, verbose=args.verbose)
    if args.cmd == "simulate-car":
        from .tools import simulate_car
        return simulate_car.run(cfg, target=args.target, port=args.port)

    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
