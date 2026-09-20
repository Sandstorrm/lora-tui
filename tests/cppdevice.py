"""Adapter: the firmware's real C++ engine (tests/harness) behind the PyDevice interface."""
from __future__ import annotations

import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "harness" / "device_harness"


class CppDevice:
    def __init__(self) -> None:
        self.p = subprocess.Popen([str(HARNESS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, bufsize=1, encoding="latin-1")
        self.stats: dict = {}

    def close(self) -> None:
        try:
            self._cmd("quit")
        finally:
            self.p.stdin.close()
            self.p.wait(timeout=5)

    def _cmd(self, line: str) -> list:
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        acts = []
        while True:
            l = self.p.stdout.readline().rstrip("\n")
            if l == "ok":
                return acts
            if l == "":
                raise RuntimeError("harness died")
            if l.startswith("send "):
                acts.append(("send", l[5:].encode("latin-1")))
            elif l.startswith("profile "):
                acts.append(("profile", int(l[8:])))
            elif l.startswith("power "):
                acts.append(("power", int(l[6:])))
            elif l.startswith("rxpush "):
                acts.append(("rxpush", bytes.fromhex(l[7:])))
            elif l.startswith("state "):
                self.stats = dict(kv.split("=") for kv in l[6:].split())

    def begin(self, now): return self._cmd(f"begin {int(now)}")
    def tick(self, now): return self._cmd(f"tick {int(now)}")
    def on_tx_done(self, now, ok=True): return self._cmd(f"txdone {int(now)} {int(ok)}")
    def on_frame(self, now, d, rssi, snr): return self._cmd(f"rx {int(now)} {rssi} {snr} {d.decode('latin-1')}")
    def cli_write(self, data): self._cmd("cli " + data.hex())
    def set_want(self, w): self._cmd(f"want {w}")
    def set_default_power(self, w): self._cmd(f"defpower {w}")
    def state(self) -> dict:
        self._cmd("state")
        return self.stats
