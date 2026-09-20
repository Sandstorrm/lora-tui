"""Virtual-time simulation of the radio link: controller engine <-> device engine.

Half-duplex radios with real airtimes, per-direction RSSI/SNR, loss, corruption, duplication and
profile-dependent reception (a frame is only heard if both radios are on the same profile for its
whole duration).  Deterministic for a given seed, and fast: it jumps from event to event.

Any object with the PyDevice interface can play the Core2 -- the pure-Python model or an adapter
around the firmware's real C++ engine (tests/harness).
"""
from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass, field
from typing import Callable, Optional

from .engine import Link, SendFrame, SetPower, SetProfile
from .protocol import HOME, PROFILES, airtime_ms

AT_PARAM_MS = 35        # AT+PARAMETER settle time
TX_OVERHEAD_MS = 35     # UART + module processing on top of the airtime (measured: 13-60 ms)
DEV_PROC_MS = 45        # device: +RCV line -> AT+SEND issued
DEV_TICK_MS = 250


@dataclass
class Channel:
    """Radio conditions. rssi/snr may be numbers or callables profile -> number."""
    loss: float = 0.0            # probability a frame vanishes
    corrupt: float = 0.0         # probability one character of a delivered frame is altered
    dup: float = 0.0             # probability a delivered frame arrives twice
    rssi_up: object = -70        # controller -> device
    rssi_dn: object = -70        # device -> controller
    snr_up: object = 8
    snr_dn: object = 8
    blackout: Optional[Callable[[float], bool]] = None   # t -> True while nothing gets through
    filter: Optional[Callable[[str, bytes, float, int], bool]] = None  # (dir, frame, t, profile) -> drop?
    enforce_sensitivity: bool = False   # drop frames weaker than the profile's sensitivity
    overload_dbm: Optional[float] = None  # receivers hotter than this lose frames (radios side by side)

    @staticmethod
    def _v(x, profile):
        return x(profile) if callable(x) else x

    def q(self, direction: str, profile: int) -> tuple[int, int]:
        return (int(self._v(getattr(self, "rssi_" + direction), profile)),
                int(self._v(getattr(self, "snr_" + direction), profile)))


# A stand-in for the Core2's menus and apps, shaped like what `screen view` really returned on hardware.
FAKE_MENUS = {
    "Main Menu": ("main", ["WiFi", "BLE", "RF", "NRF24", "LoRa", "FM", "IR", "Ethernet", "GPS", "RFID", "Files",
                           "JS Interpreter", "Clock", "Others", "Config"]),
    "WiFi": ("sub", ["Connect to Wifi", "Start WiFi AP", "Wifi Atks", "Evil Portal", "NetCut", "Listen TCP",
                     "Client TCP", "SOCKS4 Proxy", "TelNET", "SSH", "Sniffer", "Channel Analyzer", "Jam Detect",
                     "Scan Hosts", "Main Menu"]),
    "Bluetooth": ("sub", ["Media Cmds", "BLE Scan", "iBeacon", "Bad BLE", "BLE Keyboard", "BLE Spam", "Main Menu"]),
    "LoRa": ("sub", ["Range / Speed", "Chat", "Change username", "Change Frequency", "Main Menu"]),
}
FAKE_ENTRY = {("Main Menu", "WiFi"): "WiFi", ("Main Menu", "BLE"): "Bluetooth", ("Main Menu", "LoRa"): "LoRa"}
FAKE_APPS = {
    "iBeacon": ["17:51:25  CHG", "IBEACON", "UUID:e4c159a0-8c82-11e6-bdf4-0800200c9a66", "Press Any key to STOP.",
                "PREV  SEL  NEXT"],
    "Wifi Atks": ["Deauth", "Beacon Spam", "Target Atk", "Deauth Flood", "PREV  SEL  NEXT"],
    "BLE Scan": ["Scanning...", "0 devices found", "PREV  SEL  NEXT"],
}


class FakeScreen:
    def __init__(self) -> None:
        self.menu, self.sel, self.app, self.stack = "Main Menu", 0, None, []

    def json(self) -> str:
        import json
        if self.app:
            return json.dumps(dict(w=320, h=220, mode="app", title=self.menu,
                                   lines=FAKE_APPS.get(self.app, [self.app, "(nothing drawn as text)"])),
                              separators=(",", ":"))
        kind, opts = FAKE_MENUS[self.menu]
        return json.dumps(dict(w=320, h=220, mode="menu", type=kind, title=self.menu, sel=self.sel, options=opts),
                          separators=(",", ":"))

    def act(self, action: str, value: str) -> None:
        kind, opts = FAKE_MENUS[self.menu]
        if action == "open" and not self.app and value.isdigit() and int(value) < len(opts):
            label = opts[int(value)]
            if label == "Main Menu":
                self.menu, self.stack, self.sel = "Main Menu", [], 0
            elif (self.menu, label) in FAKE_ENTRY:
                self.stack.append((self.menu, int(value)))
                self.menu, self.sel = FAKE_ENTRY[(self.menu, label)], 0
            else:
                self.app = label
        elif action in ("back", "esc"):
            if self.app:
                self.app, self.menu, self.stack, self.sel = None, "Main Menu", [], 0   # like the real one: exits to main
            elif self.stack:
                self.menu, self.sel = self.stack.pop()


class FakeCli:
    """A stand-in for the Bruce CLI: line in -> text out (ends with the '# ' prompt)."""
    HELP = ("Bruce vdev\r\nThese shell commands are defined internally.\r\n\r\nWiFi Commands:\r\n"
            "  wifi off (Disconnects Wifi)\r\n  wifi on  (Connects to a known Wifi network)\r\n"
            "  arp - Starts Scan Hosts ARP Scanner\r\n\r\nIR Commands:\r\n"
            "  ir rx <timeout>      - Read an IR signal and print the dump\r\n"
            "  ir tx <protocol> <address> <decoded_value>  - Send a custom decoded IR signal.\r\n\r\n"
            "Power Management:\r\n  power <off/reboot/sleep>  - General power management.\r\n")

    def __init__(self) -> None:
        self.buf = bytearray()
        self.screen = FakeScreen()
        self.t0 = 0.0
        self.now: Callable[[], float] = lambda: 0.0

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        outs = []
        while b"\n" in self.buf:
            line, _, rest = self.buf.partition(b"\n")
            self.buf = bytearray(rest)
            outs.append(self.run(line.decode("latin-1").strip()))
        return outs

    def run(self, cmd: str) -> bytes:
        up = int(self.now() / 1000) + 3600 + 9
        if cmd == "help":
            body = self.HELP
        elif cmd == "uptime":
            body = f"Uptime: {up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}\r\n"
        elif cmd == "free":
            body = "Total heap: 231815\r\nFree heap: 117003\r\nTotal PSRAM: 4194304\r\nFree PSRAM: 4176272\r\n"
        elif cmd.startswith("screen view"):
            parts = cmd.split()
            self.screen.act(parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
            body = self.screen.json() + "\r\n"
        elif cmd.startswith("nav"):
            body = "Next Pressed\r\n"
        elif cmd == "":
            body = ""
        else:
            body = f"ERROR: Unknown command '{cmd}'\r\n"
        return body.encode() + b"# "


@dataclass(order=True)
class _Ev:
    t: float
    n: int
    kind: str = field(compare=False)
    data: tuple = field(compare=False, default=())


class Sim:
    def __init__(self, link: Link, dev, channel: Optional[Channel] = None, seed: int = 1,
                 cli: Optional[FakeCli] = None):
        self.link, self.dev = link, dev
        self.ch = channel or Channel()
        self.rng = random.Random(seed)
        self.cli = cli
        if cli:
            cli.now = lambda: self.t
        self.t = 0.0
        self._q: list[_Ev] = []
        self._n = itertools.count()
        self.c_power, self.d_power = link.ctl_power, 22
        self.c_prof = link.profile           # actual radio profiles
        self.d_prof = HOME
        self.c_ready = self.d_ready = 0.0    # radio usable from (after AT+PARAMETER)
        self.c_tx_until = self.d_tx_until = 0.0
        self.c_prof_since = self.d_prof_since = 0.0
        self.out = bytearray()               # bytes the controller received (device output)
        self.cli_in = bytearray()            # bytes the device's CLI received (controller input)
        self.log: list[tuple] = []
        self.frames_sent = {"c": 0, "d": 0}
        self.frames_lost = {"c": 0, "d": 0}
        link.on_output = self.out.extend
        self.dev_actions(dev.begin(0.0))
        self._push(DEV_TICK_MS, "dtick")
        link.start(0.0)
        self.pump_controller()

    # ---- scheduling ------------------------------------------------------------------------
    def _push(self, t: float, kind: str, *data) -> None:
        heapq.heappush(self._q, _Ev(t, next(self._n), kind, data))

    def run(self, until: float, stop: Optional[Callable[["Sim"], bool]] = None, max_steps: int = 2_000_000) -> None:
        steps = 0
        while steps < max_steps:
            steps += 1
            cand = [self._q[0].t] if self._q else []
            w = self.link.next_wakeup()
            if w is not None:
                cand.append(max(w, self.t))
            if not cand:
                break
            t = min(cand)
            if t > until:
                self.t = until
                break
            self.t = t
            while self._q and self._q[0].t <= self.t:
                self._handle(heapq.heappop(self._q))
            self.link.tick(self.t)
            self.pump_controller()
            if stop and stop(self):
                break
        else:
            raise RuntimeError("simulation did not settle (livelock?)")

    def run_for(self, ms: float, stop=None) -> None:
        self.run(self.t + ms, stop)

    # ---- events ----------------------------------------------------------------------------
    def _handle(self, e: _Ev) -> None:
        k = e.kind
        if k == "c_txdone":
            self.link.on_tx_done(self.t, True)
            self.pump_controller()
        elif k == "d_txdone":
            self.dev_actions(self.dev.on_tx_done(self.t, True))
        elif k == "to_dev":
            frame, prof, start, pw = e.data
            self._deliver_to_dev(frame, prof, start, pw)
        elif k == "to_ctl":
            frame, prof, start, pw = e.data
            self._deliver_to_ctl(frame, prof, start, pw)
        elif k == "dtick":
            self.dev_actions(self.dev.tick(self.t))
            self._push(self.t + DEV_TICK_MS, "dtick")
        elif k == "d_send":
            self._device_tx(e.data[0])

    def _rx_quality(self, direction: str, prof: int, tx_power: int) -> tuple[int, int]:
        """(rssi, snr) as the receiver's module would report them, given the sender's TX power."""
        rssi, snr = self.ch.q("up" if direction == "c>d" else "dn", prof)
        rssi_eff = rssi + (tx_power - 22)
        return int(min(0, rssi_eff)), int(min(10, snr + (tx_power - 22)))

    def _gate(self, direction: str, frame: bytes, prof: int, tx_power: int = 22) -> bool:
        """True if the frame survives the channel (before random corruption)."""
        ch = self.ch
        rssi_eff = ch.q("up" if direction == "c>d" else "dn", prof)[0] + (tx_power - 22)
        if ch.overload_dbm is not None and rssi_eff > ch.overload_dbm:
            # calibrated on real hardware: two RYLR998 side by side lost roughly a quarter of exchanges
            if self.rng.random() < min(0.3, (rssi_eff - ch.overload_dbm) / 80):
                return False
        if ch.blackout and ch.blackout(self.t):
            return False
        if ch.filter and ch.filter(direction, frame, self.t, prof):
            return False
        if ch.enforce_sensitivity:
            margin = rssi_eff - PROFILES[prof].sensitivity_dbm
            if margin < 0 or (margin < 3 and self.rng.random() < 0.5):
                return False
        return True

    def _damage(self, frame: bytes) -> Optional[bytes]:
        if self.rng.random() < self.ch.loss:
            return None
        if self.rng.random() < self.ch.corrupt and len(frame) > 1:
            i = self.rng.randrange(len(frame))
            frame = frame[:i] + bytes([frame[i] ^ 0x01 if frame[i] not in (0x7E,) else 0x21]) + frame[i + 1:]
        return frame

    # ---- controller side -------------------------------------------------------------------
    def pump_controller(self) -> None:
        for a in self.link.take_actions():
            if isinstance(a, SetPower):
                self.c_power = a.dbm
                self.c_ready = max(self.t, self.c_ready) + AT_PARAM_MS
            elif isinstance(a, SetProfile):
                self.c_prof = a.idx
                self.c_prof_since = self.c_ready = max(self.t, self.c_ready) + AT_PARAM_MS
            elif isinstance(a, SendFrame):
                start = max(self.t, self.c_ready)
                air = airtime_ms(self.c_prof, len(a.data))
                end = start + air
                self.c_tx_until = end
                self.frames_sent["c"] += 1
                self._push(end + TX_OVERHEAD_MS, "c_txdone")
                self._push(end, "to_dev", a.data, self.c_prof, start, self.c_power)

    def _deliver_to_dev(self, frame: bytes, prof: int, start: float, pw: int = 22) -> None:
        heard = (self.d_prof == prof and self.d_prof_since <= start and self.d_tx_until <= start
                 and self._gate("c>d", frame, prof, pw))
        f = self._damage(frame) if heard else None
        if f is None:
            self.frames_lost["c"] += 1
            return
        rssi, snr = self._rx_quality("c>d", prof, pw)
        self.log.append((self.t, "c>d", frame))
        self.dev_actions(self.dev.on_frame(self.t, f, rssi, snr))
        if self.rng.random() < self.ch.dup:
            self.dev_actions(self.dev.on_frame(self.t + 1, f, rssi, snr))

    # ---- device side -----------------------------------------------------------------------
    def dev_actions(self, acts: list) -> None:
        for a in acts:
            if a[0] == "send":
                self._push(self.t + DEV_PROC_MS, "d_send", a[1])
            elif a[0] == "power":
                self.d_power = a[1]
            elif a[0] == "profile":
                self.d_prof = a[1]
                self.d_prof_since = self.d_ready = max(self.t, self.d_ready) + AT_PARAM_MS
            elif a[0] == "rxpush":
                self.cli_in += a[1]
                if self.cli:
                    for reply in self.cli.feed(a[1]):
                        self.dev.cli_write(reply)

    def _device_tx(self, frame: bytes) -> None:
        start = max(self.t, self.d_ready, self.d_tx_until)
        air = airtime_ms(self.d_prof, len(frame))
        end = start + air
        self.d_tx_until = end
        self.frames_sent["d"] += 1
        self._push(end + TX_OVERHEAD_MS, "d_txdone")
        self._push(end, "to_ctl", frame, self.d_prof, start, self.d_power)

    def _deliver_to_ctl(self, frame: bytes, prof: int, start: float, pw: int = 22) -> None:
        heard = (self.c_prof == prof and self.c_prof_since <= start and self.c_tx_until <= start
                 and self._gate("d>c", frame, prof, pw))
        f = self._damage(frame) if heard else None
        if f is None:
            self.frames_lost["d"] += 1
            return
        rssi, snr = self._rx_quality("d>c", prof, pw)
        self.log.append((self.t, "d>c", frame))
        self.link.on_frame(self.t, f, rssi, snr)
        if self.rng.random() < self.ch.dup:
            self.link.on_frame(self.t + 1, f, rssi, snr)
        self.pump_controller()

    # ---- conveniences ----------------------------------------------------------------------
    def reboot_device(self, new_dev) -> None:
        """Power-cycle the Core2: fresh engine, back on the home profile."""
        self.dev = new_dev
        self.d_prof, self.d_tx_until = HOME, 0.0
        self.d_prof_since = self.d_ready = self.t
        self.dev_actions(new_dev.begin(self.t))

    def restart_controller(self, link: Link) -> None:
        """Quit and relaunch the controller app (radio comes up on the home profile)."""
        self.link = link
        link.on_output = self.out.extend
        self.c_prof, self.c_tx_until = link.profile, 0.0
        self.c_prof_since = self.c_ready = self.t
        link.start(self.t)
        self.pump_controller()

    def type(self, text: str) -> None:
        self.link.send(self.t, text.encode())

    def linked(self) -> bool:
        return self.link.state.value == "linked"
