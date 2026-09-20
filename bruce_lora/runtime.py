"""Runs the link engine in real time against a radio, on its own thread.

The UI never touches the engine: it posts commands (send / profile / auto / flush / resync / power)
and receives callbacks -- `on_output(bytes)` for what the Core2 printed, `on_events(list)` for
link events and the packet log, `on_snapshot(dict)` ~5x a second for the status panels.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable, Optional

import serial

from .engine import Link, SendFrame, SetPower, SetProfile
from .logfile import heartbeat, log, log_event


RESET_AFTER_MS = 45_000


def now_ms() -> float:
    return time.monotonic() * 1000.0


class Runtime:
    def __init__(self, link: Link, radio, on_output: Callable[[bytes], None] = lambda b: None,
                 on_events: Callable[[list], None] = lambda e: None,
                 on_snapshot: Callable[[dict], None] = lambda s: None,
                 connect: Optional[Callable[[], object]] = None, missing: str = "no radio module"):
        """`radio` may be None (module not there at launch); `connect()` then tries to open it, raising if it can't.
        A module that disappears later (USB unplugged) is handled the same way: keep running, keep retrying."""
        self.link, self.radio = link, radio
        self.connect = connect
        self.module_error: Optional[str] = None if radio is not None else missing
        self.on_events, self.on_snapshot = on_events, on_snapshot
        link.on_output = on_output
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    # ---- thread-safe API for the UI --------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="link-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def send(self, text: str) -> None: self._cmds.put(("send", text.encode("utf-8", "replace")))
    def send_bytes(self, data: bytes) -> None: self._cmds.put(("send", data))
    def flush(self) -> None: self._cmds.put(("flush",))
    def set_profile(self, idx: int) -> None: self._cmds.put(("profile", idx))
    def set_auto(self, on: bool) -> None: self._cmds.put(("auto", on))
    def resync(self) -> None: self._cmds.put(("resync",))
    def set_power(self, who: str, dbm: int) -> None: self._cmds.put(("power", who, dbm))
    def set_auto_power(self, on: bool) -> None: self._cmds.put(("autopower", on))

    # ---- module presence -------------------------------------------------------------------
    @property
    def module_ok(self) -> bool:
        return self.radio is not None

    def _lose_module(self, why: str) -> None:
        log.warning("radio module lost: %s", why)
        try:
            if self.radio is not None:
                self.radio.close()
        except Exception:
            pass
        self.radio = None
        self.module_error = why
        self.link.event(now_ms(), "module", ok=False, why=why)

    def _try_connect(self) -> None:
        if self.connect is None:
            return
        try:
            radio = self.connect()
        except BaseException as e:                  # SystemExit from open_radio too
            self.module_error = str(e).splitlines()[0] if str(e) else type(e).__name__
            return
        self.radio, self.module_error = radio, None
        log.info("radio module connected on %s", getattr(radio, "port", "?"))
        self.link.resync(now_ms())
        self.link.event(now_ms(), "module", ok=True, why="")

    # ---- the loop --------------------------------------------------------------------------
    def _apply(self, cmd: tuple) -> None:
        now = now_ms()
        k = cmd[0]
        log.info("user %s %s", k, cmd[1:] if k != "send" else repr(cmd[1][:80]))
        if k == "send": self.link.send(now, cmd[1])
        elif k == "flush": self.link.flush(now)
        elif k == "profile": self.link.set_profile(now, cmd[1])
        elif k == "auto": self.link.set_auto(now, cmd[1])
        elif k == "resync": self.link.resync(now)
        elif k == "power": self.link.set_power(now, cmd[1], cmd[2])
        elif k == "autopower": self.link.set_auto_power(now, cmd[1])

    def _do_actions(self) -> None:
        for a in self.link.take_actions():
            if isinstance(a, SetProfile):
                if not self.radio.set_profile(a.idx):
                    log.warning("radio refused profile %d (at_errors=%s)", a.idx, getattr(self.radio, "at_errors", "?"))
            elif isinstance(a, SetPower):
                if not self.radio.set_power(a.dbm):
                    log.warning("radio refused power %d dBm", a.dbm)
            elif isinstance(a, SendFrame):
                ok = self.radio.send_frame(a.data)
                if not ok:
                    log.warning("AT+SEND failed (%dB on profile %d, at_errors=%s)", len(a.data), a.profile,
                                getattr(self.radio, "at_errors", "?"))
                self.link.on_tx_done(now_ms(), ok)

    def _run(self) -> None:
        try:
            log.info("runtime start: port=%s profile=%s auto=%s", getattr(self.radio, "port", "?"),
                     self.link.profile, self.link.cfg.auto)
            self.link.start(now_ms())
            last_snap = 0.0
            last_hb = now_ms()
            search_since: Optional[float] = now_ms()
            last_reset = now_ms()
            last_try = 0.0
            while not self._stop.is_set():
                while True:
                    try:
                        self._apply(self._cmds.get_nowait())
                    except queue.Empty:
                        break
                now = now_ms()
                if self.radio is None:
                    if now - last_try > 1500:
                        last_try = now
                        self._try_connect()
                    else:
                        time.sleep(0.05)
                elif not os.path.exists(getattr(self.radio, "port", "/")) and hasattr(self.radio, "ser"):
                    self._lose_module("the module was unplugged")
                if self.radio is not None:
                    try:
                        self.link.tick(now)
                        self._do_actions()
                        w = self.link.next_wakeup()
                        wait = 0.02 if w is None else max(0.0, min(0.02, (w - now_ms()) / 1000.0))
                        rx = self.radio.poll(wait)
                        if rx is not None:
                            self.link.on_frame(now_ms(), rx.data, rx.rssi, rx.snr)
                            self.link.tick(now_ms())
                            self._do_actions()
                    except (OSError, serial.SerialException) as e:
                        self._lose_module(f"serial port failed ({type(e).__name__})")
                # Deaf-but-alive modules look like "no Core2 around": after a long search, reset ours.
                if self.link.state.value == "linked":
                    search_since = None
                elif search_since is None:
                    search_since = now_ms()
                elif (now_ms() - search_since > RESET_AFTER_MS and now_ms() - last_reset > RESET_AFTER_MS
                      and self.radio is not None and hasattr(self.radio, "reset")):
                    last_reset = now_ms()
                    log.warning("no Core2 found for %.0f s: resetting the controller radio module", RESET_AFTER_MS / 1000)
                    try:
                        self.radio.reset()
                        self.link.event(now_ms(), "notice", text="searched for a long time: reset the radio module")
                    except (OSError, serial.SerialException) as e:
                        self._lose_module(f"serial port failed ({type(e).__name__})")
                ev = self.link.take_events()
                if ev:
                    for e in ev:
                        log_event(e)
                    self.on_events(ev)
                if now_ms() - last_hb > 30_000:
                    last_hb = now_ms()
                    heartbeat(self.link.snapshot() | {"at_errors": getattr(self.radio, "at_errors", None),
                                                      "module": self.radio is not None})
                if now - last_snap > 200:
                    last_snap = now
                    snap = self.link.snapshot()
                    snap.update(now=now_ms(), power=getattr(self.radio, "power", None), at_errors=getattr(self.radio, "at_errors", 0),
                                port=getattr(self.radio, "port", "demo"), module=self.radio is not None,
                                module_error=self.module_error)
                    if self.radio is None:
                        snap.update(state="nomodule", dn=(None, None), up=(None, None), margin=None, rtt_ms=0)
                    self.on_snapshot(snap)
        except Exception as e:                      # surface it in the UI instead of dying silently
            log.exception("runtime crashed")
            self.error = f"{type(e).__name__}: {e}"
            self.on_events([])
            raise
        finally:
            log.info("runtime stopped")
            if self.radio is not None:
                self.radio.close()
