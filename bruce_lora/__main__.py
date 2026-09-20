"""bruce-lora: remote control for a Bruce device over LoRa.

    python -m bruce_lora                 full-screen TUI (auto-detects the controller module)
    python -m bruce_lora --demo          TUI against a simulated Core2, no hardware
    python -m bruce_lora --plain         line-mode terminal (no TUI)
    python -m bruce_lora --exec uptime --exec free     run commands, print output, exit
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from .engine import Link, LinkConfig
from .protocol import PROFILES


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="bruce-lora", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port of the controller RYLR998 (default: auto-detect)")
    ap.add_argument("--demo", action="store_true", help="simulated Core2, no hardware needed")
    ap.add_argument("--plain", action="store_true", help="plain line-mode terminal instead of the TUI")
    ap.add_argument("--exec", action="append", metavar="CMD", help="run CMD on the device, print its output, exit")
    ap.add_argument("--profile", default="auto", help="auto, or 0-5 / a name (Range Long Standard Fast Quick Turbo)")
    ap.add_argument("--power", type=int, default=None, help="controller TX power in dBm, 0-22 (default 22)")
    ap.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for the link / each command (--exec)")
    ap.add_argument("--band", type=float, default=915.0, help="frequency in MHz (default 915)")
    ap.add_argument("--no-log", action="store_true", help="don't write the session log")
    return ap.parse_args(argv)


def profile_arg(text: str):
    """-> (auto, index)"""
    t = text.strip().lower()
    if t in ("auto", "a"):
        return True, 0
    if t.isdigit() and int(t) < len(PROFILES):
        return False, int(t)
    for p in PROFILES:
        if p.name.lower() == t:
            return False, p.idx
    raise SystemExit(f"unknown profile {text!r}")


def open_radio(args):
    from .radio import Rylr998, RadioError, find_controller_port
    port = args.port or find_controller_port()
    if not port:
        raise SystemExit("no serial port found -- plug in the controller RYLR998 or pass --port (try --demo)")
    try:
        return Rylr998(port, band_hz=int(args.band * 1_000_000), power=22 if args.power is None else args.power)
    except RadioError as e:
        raise SystemExit(f"{e}\n(is another program holding {port}?)")


def try_open_radio(args):
    """Like open_radio, but returns (radio, None) or (None, reason) instead of exiting."""
    try:
        return open_radio(args), None
    except SystemExit as e:
        return None, str(e).splitlines()[0]
    except OSError as e:                                    # includes serial.SerialException
        return None, f"cannot open {args.port}: {e}"



def make_link(args, radio=None) -> Link:
    auto, idx = profile_arg(args.profile)
    # an explicit --power pins this side's TX power; otherwise power control is automatic
    cfg = LinkConfig(auto=auto, target=idx, auto_power=args.power is None)
    return Link(cfg, initial_power=radio.power if radio is not None else 22)


def run_exec(args) -> int:
    from .runtime import Runtime
    radio = open_radio(args)
    link = make_link(args, radio)
    out = bytearray()
    rt = Runtime(link, radio, on_output=out.extend)
    rt.start()
    t0 = time.monotonic()
    while link.state.value != "linked":
        if time.monotonic() - t0 > args.timeout:
            print("no link", file=sys.stderr)
            rt.stop()
            return 2
        time.sleep(0.05)
    print(f"# linked in {time.monotonic() - t0:.1f}s on {PROFILES[link.profile].name}", file=sys.stderr)
    rc = 0
    for cmd in args.exec:
        out.clear()
        t1 = time.monotonic()
        rt.send(cmd + "\n")
        while not out.endswith(b"# "):
            if time.monotonic() - t1 > args.timeout:
                print(f"# timeout waiting for output of {cmd!r}", file=sys.stderr)
                rc = 1
                break
            time.sleep(0.02)
        text = out.decode("utf-8", "replace").replace("\r\n", "\n")
        sys.stdout.write(f"$ {cmd}\n{text}\n")
        s = link.snapshot()
        print(f"# {time.monotonic() - t1:.1f}s  {PROFILES[s['profile']].name}  rssi {s['dn'][0]} snr {s['dn'][1]} "
              f"retries {s['retries']} timeouts {s['timeouts']} bad {s['bad']}", file=sys.stderr)
    rt.stop()
    return rc


def run_plain(args) -> int:
    from .runtime import Runtime
    radio = open_radio(args)
    link = make_link(args, radio)
    rt = Runtime(link, radio,
                 on_output=lambda b: (sys.stdout.write(b.decode("utf-8", "replace").replace("\r\n", "\n")),
                                      sys.stdout.flush()))
    rt.start()
    print("bruce-lora (plain). Ctrl-D to quit. Lines starting with ':' are link commands "
          "(:profile N|auto, :flush, :resync, :status).", file=sys.stderr)
    try:
        for line in sys.stdin:
            line = line.rstrip("\n")
            if line.startswith(":"):
                parts = line[1:].split()
                if parts[:1] == ["profile"] and len(parts) > 1:
                    auto, idx = profile_arg(parts[1])
                    rt.set_auto(True) if auto else rt.set_profile(idx)
                elif parts[:1] == ["flush"]:
                    rt.flush()
                elif parts[:1] == ["resync"]:
                    rt.resync()
                elif parts[:1] == ["status"]:
                    s = link.snapshot()
                    print({k: s[k] for k in ("state", "profile", "dn", "up", "retries", "timeouts")}, file=sys.stderr)
            else:
                rt.send(line + "\n")
    except KeyboardInterrupt:
        pass
    rt.stop()
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.demo and not args.no_log:
        from .logfile import setup_logging
        args.log_path = setup_logging()
        if not args.exec:
            print(f"session log: {args.log_path}", file=sys.stderr)
    if args.exec:
        return run_exec(args)
    if args.plain:
        return run_plain(args)
    from .tui import run_tui                    # the full-screen UI
    return run_tui(args)


if __name__ == "__main__":
    raise SystemExit(main())
