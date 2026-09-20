"""A simulated Core2 behind the same interface as Runtime, so the TUI can run with no hardware.

The virtual radio "walks" toward and away from the Core2 so auto range/speed and power control
have something to react to: right next to it (radios touching), across a room, then far away.
"""
from __future__ import annotations

import math
import queue
import random
import threading
import time
from typing import Callable, Optional

from ..engine import Link, LinkConfig
from ..protocol import PROFILES
from ..sim import Channel, FakeCli, Sim
from ..simdevice import PyDevice

# (seconds into the walk, RSSI the link would have at full power)
WALK = [(0, 8), (25, 8), (55, -62), (95, -92), (140, -112), (185, -120), (230, -92), (270, -55), (310, 8)]


def walk_rssi(t: float) -> float:
    t = t % WALK[-1][0]
    for (t0, r0), (t1, r1) in zip(WALK, WALK[1:]):
        if t0 <= t <= t1:
            return r0 + (r1 - r0) * (t - t0) / (t1 - t0)
    return WALK[-1][1]


class DemoRuntime:
    port = "demo"

    def __init__(self, link: Optional[Link] = None, speed: float = 3.0, walk: bool = True, seed: int = 11,
                 on_output: Callable[[bytes], None] = lambda b: None,
                 on_events: Callable[[list], None] = lambda e: None,
                 on_snapshot: Callable[[dict], None] = lambda s: None):
        self.link = link or Link(LinkConfig(auto=True))
        self.speed = speed
        self.on_events, self.on_snapshot = on_events, on_snapshot
        self.rng = random.Random(seed)
        self._t0 = time.monotonic()
        self.ch = Channel(loss=0.03, corrupt=0.02, enforce_sensitivity=True, overload_dbm=-20)
        self._walk = walk
        self._set_rssi(8 if walk else -60)
        self.cli = FakeCli()
        self.sim = Sim(self.link, PyDevice(), self.ch, seed=seed, cli=self.cli)
        self.link.on_output = self._out_wrap(on_output)
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    def _out_wrap(self, cb):
        def out(b: bytes) -> None:
            cb(b)
        return out

    def _set_rssi(self, r: float) -> None:
        self.ch.rssi_up = self.ch.rssi_dn = r
        # SNR above the noise floor (125 kHz: about -117 dBm), narrower for wider bandwidths, clipped like the module
        self.ch.snr_up = self.ch.snr_dn = lambda p, r=r: min(10, r + 117 - 10 * math.log10(PROFILES[p].bw_hz / 125_000))

    # ---- same surface as Runtime -----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="demo-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def send(self, text: str) -> None: self._cmds.put(("send", text.encode()))
    def send_bytes(self, data: bytes) -> None: self._cmds.put(("send", data))
    def flush(self) -> None: self._cmds.put(("flush",))
    def set_profile(self, idx: int) -> None: self._cmds.put(("profile", idx))
    def set_auto(self, on: bool) -> None: self._cmds.put(("auto", on))
    def resync(self) -> None: self._cmds.put(("resync",))
    def set_power(self, who: str, dbm: int) -> None: self._cmds.put(("power", who, dbm))
    def set_auto_power(self, on: bool) -> None: self._cmds.put(("autopower", on))

    def _apply(self, c: tuple) -> None:
        t, k = self.sim.t, c[0]
        if k == "send": self.link.send(t, c[1])
        elif k == "flush": self.link.flush(t)
        elif k == "profile": self.link.set_profile(t, c[1])
        elif k == "auto": self.link.set_auto(t, c[1])
        elif k == "resync": self.link.resync(t)
        elif k == "power": self.link.set_power(t, c[1], c[2])
        elif k == "autopower": self.link.set_auto_power(t, c[1])

    def _run(self) -> None:
        last_snap = 0.0
        real0 = time.monotonic()
        try:
            while not self._stop.is_set():
                while True:
                    try:
                        self._apply(self._cmds.get_nowait())
                    except queue.Empty:
                        break
                vt = (time.monotonic() - real0) * 1000 * self.speed
                if self._walk:
                    self._set_rssi(walk_rssi(vt / 1000 / 1.0))
                self.sim.run(vt)
                ev = self.link.take_events()
                if ev:
                    self.on_events(ev)
                now = time.monotonic()
                if now - last_snap > 0.2:
                    last_snap = now
                    snap = self.link.snapshot()
                    snap.update(now=self.sim.t, power=self.sim.c_power, at_errors=0, port="demo",
                                demo_rssi=self.ch.rssi_dn, virtual_s=vt / 1000)
                    self.on_snapshot(snap)
                time.sleep(0.02)
        except Exception as e:                  # pragma: no cover
            self.error = f"{type(e).__name__}: {e}"
            raise
