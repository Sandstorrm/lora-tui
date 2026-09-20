"""Controller-side link engine (protocol v1) -- sans-I/O.

The runtime feeds it received frames, TX-done notices and clock ticks, and executes the
actions it queues (send a frame / set a radio profile).  It never sleeps or touches a port,
so tests can drive it with a virtual clock against the firmware's real C++ engine.

The controller is the master: it hunts for the device, then runs strictly alternating
exchanges (its frame -> the device's reply), retransmitting on timeout.  See PROTOCOL.md.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from . import protocol as pr
from .auto import AutoPolicy
from .protocol import (F_FLUSH, F_SYNC, HOME, MAX_POWER, N_PROFILES, WANT_AUTO, Frame, airtime_ms, data_budget,
                       decode, encode, t_confirm_ms, t_wait_ms, take_escaped)


class State(str, Enum):
    SEARCH = "search"
    LINKED = "linked"


@dataclass
class SendFrame:
    data: bytes
    profile: int


@dataclass
class SetProfile:
    idx: int


@dataclass
class SetPower:
    dbm: int


@dataclass
class Event:
    kind: str
    t: float
    info: dict = field(default_factory=dict)


@dataclass
class LinkConfig:
    auto: bool = True              # pick the profile from measured link margin
    target: int = HOME             # manual profile (used when auto is off)
    follow_device_want: bool = True  # obey a profile chosen on the Core2's own slider
    hint_profile: int = HOME       # profile to try first when hunting
    guard_ms: float = pr.T_GUARD_MS
    active_window_ms: float = 15_000
    active_poll_ms: float = 400
    idle_poll_ms: float = 4_000
    max_tries: int = 5
    hunt_tries: int = 2
    confirm_tries: int = 3
    auto_hold_ms: float = 8_000
    bad_profile_ms: float = 120_000
    auto_power: bool = True        # keep both radios below the receivers' overload level
    power_hold_ms: float = 3_000


@dataclass
class _Inflight:
    kind: str                       # 'H' hello, 'D' data/poll, 'P' profile request
    tries: int = 0
    deadline: Optional[float] = None
    tx_deadline: float = 0.0        # give up waiting for the radio's "+OK" after this
    sent_profile: int = HOME
    last_tx: float = 0.0


@dataclass
class _Switch:
    frm: int
    to: int
    stage: str = "request"          # 'request' -> 'confirm'
    confirm_fail: int = 0
    confirm_started: float = 0.0


@dataclass
class _Seg:
    raw: bytes
    flags: int


class Link:
    def __init__(self, cfg: Optional[LinkConfig] = None, rng: Optional[random.Random] = None,
                 on_output: Optional[Callable[[bytes], None]] = None, initial_profile: int = HOME,
                 initial_power: int = MAX_POWER):
        self.cfg = cfg or LinkConfig()
        self.rng = rng or random.Random()
        self.on_output = on_output or (lambda b: None)
        self.policy = AutoPolicy()
        self.actions: list = []
        self.events: list[Event] = []

        self.state = State.SEARCH
        self.profile = initial_profile          # what the radio is set to, as far as we know
        self.goal = self.cfg.target if not self.cfg.auto else initial_profile
        self.sid = 0
        self.inflight: Optional[_Inflight] = None
        self.busy_tx = False
        self.next_at = 0.0
        self.last_reply_at = 0.0
        self.last_activity = 0.0
        self.last_switch_at = -1e12
        self.switch_hold_until = 0.0
        self.sw: Optional[_Switch] = None
        self.bad: dict[int, float] = {}
        self.hunt: list[int] = []
        self.hunt_i = 0
        self.resume_ok = False      # we hold a session the device may still remember

        # TX power (dBm): ours is set directly on the radio, the Core2's through 'T'/'U' exchanges
        self.ctl_power = initial_power
        self.dev_power = MAX_POWER
        self.dev_goal = MAX_POWER
        self.dev_manual: Optional[int] = None
        self.dev_auto_goal: Optional[int] = None    # the power auto-control last chose for the Core2
        self.last_power_at = -1e12

        # data channel
        self.txq = bytearray()
        self.flush_req = False
        self.pend: Optional[_Seg] = None
        self.snd_seq = 0
        self.rcv_next = 0
        self.last_want = WANT_AUTO
        self.more = False

        # statistics (for the UI)
        self.tx_frames = self.rx_frames = self.bad_frames = self.timeouts = self.retries = 0
        self.dup_segments = self.bytes_in = self.bytes_out = self.exchanges = 0
        self.rtt_ms = 0.0
        self.fail_ewma = 0.0
        self.dn = (None, None)                  # (rssi, snr) measured here
        self.up = (None, None)                  # (rssi, snr) the device reports
        self.link_since: Optional[float] = None
        self.device_want = WANT_AUTO

    # ---- public API ------------------------------------------------------------------------

    def start(self, now: float) -> None:
        self.resume_ok = False
        self._enter_search(now, hint=self.cfg.hint_profile)

    def take_actions(self) -> list:
        a, self.actions = self.actions, []
        return a

    def take_events(self) -> list[Event]:
        e, self.events = self.events, []
        return e

    def next_wakeup(self) -> Optional[float]:
        inf = self.inflight
        if inf is not None:
            return inf.deadline if inf.deadline is not None else inf.tx_deadline
        return None if self.busy_tx else self.next_at

    def send(self, now: float, data: bytes) -> None:
        self.txq += data
        self.last_activity = now
        self._kick(now)

    def flush(self, now: float) -> None:
        """Ask the device to drop its queued output (Ctrl-C for the output stream)."""
        self.flush_req = True
        self.last_activity = now
        self._kick(now)

    def set_profile(self, now: float, idx: int) -> None:
        self.cfg.auto = False
        self.cfg.target = self.goal = idx
        self.bad.pop(idx, None)
        self.switch_hold_until = 0.0
        self.event(now, "mode", auto=False, target=idx)
        self._kick(now)

    def set_auto(self, now: float, on: bool) -> None:
        self.cfg.auto = on
        if on:
            self.policy.up_streak = 0
        else:
            self.cfg.target = self.goal = self.profile
        self.event(now, "mode", auto=on, target=self.cfg.target)

    def set_power(self, now: float, who: str, dbm: int) -> None:
        """Manual TX power for 'ctl' (this controller) or 'dev' (the Core2); turns auto power off."""
        dbm = max(0, min(MAX_POWER, dbm))
        self.cfg.auto_power = False
        if who == "ctl":
            self._apply_ctl_power(now, dbm)
        else:
            self.dev_manual = self.dev_goal = dbm
        self.event(now, "mode", auto_power=False)
        self._kick(now)

    def set_auto_power(self, now: float, on: bool) -> None:
        self.cfg.auto_power = on
        if on:
            self.dev_manual = None
            self.last_power_at = -1e12
        self.event(now, "mode", auto_power=on)

    def _apply_ctl_power(self, now: float, dbm: int) -> None:
        if dbm != self.ctl_power:
            self.ctl_power = dbm
            self.actions.append(SetPower(dbm))
            self.event(now, "power", who="ctl", dbm=dbm)

    def resync(self, now: float) -> None:
        self.event(now, "notice", text="resync requested")
        self._enter_search(now, hint=self.profile)

    def queued_out(self) -> int:
        return len(self.txq) + (len(self.pend.raw) if self.pend else 0)

    def snapshot(self) -> dict:
        m = self.policy.margin(self.profile)
        return dict(state=self.state.value, profile=self.profile, goal=self.goal, auto=self.cfg.auto,
                    dn=self.dn, up=self.up, margin=m, rtt_ms=self.rtt_ms, tx=self.tx_frames,
                    rx=self.rx_frames, bad=self.bad_frames, timeouts=self.timeouts, retries=self.retries,
                    bytes_in=self.bytes_in, bytes_out=self.bytes_out, link_since=self.link_since,
                    switching=self.sw is not None, device_want=self.device_want, sid=self.sid,
                    queued=self.queued_out(), fail=self.fail_ewma, dev_power=self.dev_power,
                    ctl_power=self.ctl_power, auto_power=self.cfg.auto_power)

    # ---- radio inputs ----------------------------------------------------------------------

    def on_tx_done(self, now: float, ok: bool = True) -> None:
        self.busy_tx = False
        inf = self.inflight
        if inf is not None and inf.deadline is None:
            inf.deadline = now + t_wait_ms(inf.sent_profile) if ok else now

    def on_frame(self, now: float, data: bytes, rssi: int, snr: int) -> None:
        f = decode(data, from_device=True)
        self.event(now, "rx", raw=data, frame=f, rssi=rssi, snr=snr, profile=self.profile)
        if f is None:
            self.bad_frames += 1
            return
        self.rx_frames += 1
        if f.sid != self.sid:
            return
        if self.state is State.SEARCH:
            inf = self.inflight
            if f.kind == "W" and inf is not None and inf.kind == "H":
                self._linked(now, f, rssi, snr)
            return
        if f.kind == "N":
            self._session_lost(now, "the Core2 dropped the session")
        elif f.kind == "Q":
            self._on_switch_ack(now, f, rssi, snr)
        elif f.kind == "U":
            self._on_power_ack(now, f, rssi, snr)
        elif f.kind == "D":
            self._on_data(now, f, rssi, snr)

    def tick(self, now: float) -> None:
        inf = self.inflight
        if inf is not None:
            if inf.deadline is not None:
                if now >= inf.deadline:
                    self._on_timeout(now)
            elif now >= inf.tx_deadline:            # radio never confirmed the TX
                self._on_timeout(now)
            return
        if self.busy_tx or now < self.next_at:
            return
        if self.state is State.SEARCH:
            self._hunt_send(now)
        else:
            self._linked_send(now)

    # ---- helpers ---------------------------------------------------------------------------

    def event(self, now: float, kind: str, **info) -> None:
        self.events.append(Event(kind, now, info))

    def _kick(self, now: float) -> None:
        if self.state is State.LINKED and self.inflight is None and not self.busy_tx:
            self.next_at = min(self.next_at, max(now, self.last_reply_at + self.cfg.guard_ms))

    def _new_sid(self) -> int:
        return self.rng.randrange(1, 16)

    def _set_radio(self, idx: int) -> None:
        if idx != self.profile:
            self.actions.append(SetProfile(idx))
            self.profile = idx

    def _tx(self, now: float, frame: Frame, kind: str, tries: int = 0) -> None:
        data = encode(frame, from_device=False)
        self.actions.append(SendFrame(data, self.profile))
        self.busy_tx = True
        self.tx_frames += 1
        inf = self.inflight if (self.inflight and self.inflight.kind == kind) else _Inflight(kind)
        inf.tries = tries
        inf.deadline = None
        inf.sent_profile = self.profile
        inf.last_tx = now
        inf.tx_deadline = now + airtime_ms(self.profile, len(data)) + 3000
        self.inflight = inf
        self.event(now, "tx", raw=data, frame=frame, profile=self.profile, retry=tries)

    # ---- searching -------------------------------------------------------------------------

    def _enter_search(self, now: float, hint: Optional[int] = None) -> None:
        """(Re)connect.  If we hold a session the device may still remember, HELLO resumes it
        (same sid, fresh=0) so nothing in flight is lost; otherwise it starts a new one."""
        if self.state is State.LINKED:
            self.link_since = None
        self.state = State.SEARCH
        self.sw = None
        self.more = False
        self.inflight = None
        if not self.resume_ok:
            self.sid = self._new_sid()
            self._reset_channel()
        first = self.cfg.hint_profile if hint is None else hint
        self.hunt = [first] + [p for p in [HOME, 1, 2, 3, 4, 5] if p != first]
        self.hunt_i = 0
        self.next_at = now
        self.event(now, "state", state="search")

    def _reset_channel(self) -> None:
        """Start the byte streams over; a segment that was in flight goes back to the queue."""
        if self.pend is not None:
            self.txq[:0] = self.pend.raw
            self.pend = None
        self.snd_seq = self.rcv_next = 0
        self.more = False

    def _hunt_send(self, now: float) -> None:
        cand = self.hunt[self.hunt_i % len(self.hunt)]
        self._set_radio(cand)
        self.inflight = None
        self._tx(now, Frame("H", sid=self.sid, fresh=0 if self.resume_ok else 1), "H")

    def _hunt_advance(self, now: float) -> None:
        self.inflight = None
        self.hunt_i += 1
        self.next_at = now + 40

    def _linked(self, now: float, f: Frame, rssi: int, snr: int) -> None:
        resumed = bool(f.res) and self.resume_ok
        self.state = State.LINKED
        self.inflight = None
        if not resumed:
            self._reset_channel()           # the device started over: so do we (in-flight data is re-queued)
            self.policy.reset()
            self.last_want = f.want
        self.resume_ok = True
        self.link_since = now
        self.last_reply_at = self.last_activity = now
        self.next_at = now + self.cfg.guard_ms
        self.cfg.hint_profile = self.profile
        self.device_want = f.want
        self.dev_power = f.power        # the device restores its default power on every HELLO ...
        if not self.cfg.auto_power and self.dev_manual is not None:
            self.dev_goal = self.dev_manual                     # ... so put back what we had chosen, right away
        elif self.cfg.auto_power and resumed and self.dev_auto_goal is not None:
            self.dev_goal = self.dev_auto_goal
        else:
            self.dev_goal = f.power
        self._quality(rssi, snr, f)
        if not resumed:
            if self.cfg.follow_device_want and f.want != WANT_AUTO and f.want < N_PROFILES:
                self.cfg.auto = False
                self.cfg.target = self.goal = f.want
            else:
                self.goal = self.profile if self.cfg.auto else self.cfg.target
        self.event(now, "state", state="linked", profile=self.profile, resumed=resumed)

    def _session_lost(self, now: float, why: str) -> None:
        self.event(now, "notice", text=why)
        self._enter_search(now, hint=self.profile)

    def _quality(self, rssi: int, snr: int, f: Frame) -> None:
        self.dn = (rssi, snr)
        self.up = (f.rssi, f.snr)
        self.policy.observe("dn", self.profile, rssi, snr, self.dev_power)
        self.policy.observe("up", self.profile, f.rssi, f.snr, self.ctl_power)

    # ---- linked ----------------------------------------------------------------------------

    def _desired(self, now: float) -> int:
        return self.goal

    def _maybe_start_switch(self, now: float) -> None:
        if self.sw is not None or now < self.switch_hold_until:
            return
        want = self._desired(now)
        if want == self.profile or not 0 <= want < N_PROFILES:
            return
        if self.bad.get(want, 0) > now:      # failed recently; set_profile() clears this for manual picks
            return
        self.sw = _Switch(frm=self.profile, to=want)
        self.event(now, "switching", frm=self.profile, to=want)

    def _linked_send(self, now: float) -> None:
        self._maybe_start_switch(now)
        if self.sw is not None and self.sw.stage == "request":
            self._send_switch_request(now, tries=0)
            return
        if self.sw is None and self.dev_goal != self.dev_power:
            self._send_power_request(now, tries=0)
            return
        self._send_data(now, tries=0)

    def _send_power_request(self, now: float, tries: int) -> None:
        self._tx(now, Frame("T", sid=self.sid, power=self.dev_goal), "T", tries)

    def _on_power_ack(self, now: float, f: Frame, rssi: int, snr: int) -> None:
        inf = self.inflight
        if inf is None or inf.kind != "T" or f.power != self.dev_goal:
            return
        self.dev_power = f.power
        self.inflight = None
        self.last_reply_at = self.last_power_at = now
        self.next_at = now + self.cfg.guard_ms
        self.event(now, "power", who="dev", dbm=f.power)

    def _send_switch_request(self, now: float, tries: int) -> None:
        sw = self.sw
        assert sw is not None
        at = sw.frm if tries % 2 == 0 else sw.to     # retries alternate: old, new, old, ...
        self._set_radio(at)
        self._tx(now, Frame("P", sid=self.sid, target=sw.to), "P", tries)

    def _build_data(self) -> Frame:
        if self.pend is None and (self.txq or self.flush_req):
            flags = 0
            budget = data_budget(self.profile, from_device=False)
            if self.flush_req:
                flags |= F_FLUSH
                self.flush_req = False
                raw = bytes(self.txq[:budget]) or b"\n"   # a flush must carry >= 1 byte
                from_q = bool(self.txq)
            else:
                raw = bytes(self.txq[:budget])
                from_q = True
            _, used = take_escaped(raw, budget)
            if from_q:
                del self.txq[:used]
            self.pend = _Seg(raw[:used], flags)
            self.bytes_out += used
        seg = self.pend
        return Frame("D", sid=self.sid, seq=self.snd_seq, ack=self.rcv_next,
                     flags=seg.flags if seg else 0, payload=seg.raw if seg else b"")

    def _send_data(self, now: float, tries: int) -> None:
        self.inflight = self.inflight if (self.inflight and self.inflight.kind == "D") else None
        self._tx(now, self._build_data(), "D", tries)

    def _on_switch_ack(self, now: float, f: Frame, rssi: int, snr: int) -> None:
        sw, inf = self.sw, self.inflight
        if sw is None or inf is None or inf.kind != "P" or f.target != sw.to or sw.stage != "request":
            return
        self._set_radio(sw.to)
        sw.stage = "confirm"
        sw.confirm_started = now
        self.inflight = None
        self.last_reply_at = now
        self.next_at = now + self.cfg.guard_ms
        self.up = (f.rssi, f.snr)

    def _switch_complete(self, now: float) -> None:
        sw = self.sw
        assert sw is not None
        self.sw = None
        self.last_switch_at = now
        self.cfg.hint_profile = self.profile
        self.event(now, "profile", frm=sw.frm, to=sw.to)

    def _switch_failed(self, now: float) -> None:
        sw = self.sw
        assert sw is not None
        self.sw = None
        self.inflight = None
        self._set_radio(sw.frm)
        self.bad[sw.to] = now + self.cfg.bad_profile_ms
        if self.goal == sw.to:
            self.goal = sw.frm
        hold = sw.confirm_started + t_confirm_ms(sw.to) + 300   # the device reverts at its deadline
        self.switch_hold_until = hold
        self.next_at = max(now + self.cfg.guard_ms, hold)
        self.last_switch_at = now
        self.event(now, "switch_failed", frm=sw.frm, to=sw.to)

    def _on_data(self, now: float, f: Frame, rssi: int, snr: int) -> None:
        inf = self.inflight
        # The exchange completes with any valid data reply, even a late one for an earlier try.
        if self.pend is not None and f.ack == (self.snd_seq + 1) & 15:
            self.pend = None
            self.snd_seq = (self.snd_seq + 1) & 15
        got = False
        if f.payload:
            if f.flags & F_SYNC and f.seq != (self.rcv_next - 1) & 15:
                self.rcv_next = f.seq
            if f.seq == self.rcv_next:
                self.rcv_next = (self.rcv_next + 1) & 15
                self.bytes_in += len(f.payload)
                self.on_output(f.payload)
                got = True
                self.last_activity = now
            elif f.seq == (self.rcv_next - 1) & 15:
                self.dup_segments += 1
        self.more = bool(f.flags & pr.F_MORE)
        self._quality(rssi, snr, f)
        self.device_want = f.want
        if f.want != self.last_want:
            self.last_want = f.want
            if self.cfg.follow_device_want and f.want != WANT_AUTO and f.want < N_PROFILES:
                self.event(now, "notice", text=f"Core2 requested {pr.PROFILES[f.want].name}")
                self.cfg.auto = False
                self.cfg.target = self.goal = f.want
                self.bad.pop(f.want, None)
                self.switch_hold_until = 0.0

        if inf is not None and inf.kind == "D":     # (a stray late reply must not cancel a pending 'P')
            self.exchanges += 1
            retried = inf.tries > 0
            self.rtt_ms = (now - inf.last_tx) if self.rtt_ms == 0 else self.rtt_ms * 0.7 + (now - inf.last_tx) * 0.3
            self.fail_ewma = self.fail_ewma * 0.85 + (0.15 if retried else 0.0)
            self.inflight = None
        self.last_reply_at = now
        if self.sw is not None and self.sw.stage == "confirm":
            self._switch_complete(now)
        self._schedule(now, got)
        self._auto(now)
        self._auto_power(now)

    def _schedule(self, now: float, got_data: bool) -> None:
        c = self.cfg
        busy = got_data or self.more or self.pend is not None or bool(self.txq) or self.flush_req \
            or (self.sw is not None)
        if busy:
            delay = c.guard_ms
        elif now - self.last_activity < c.active_window_ms:
            delay = c.active_poll_ms
        else:
            delay = c.idle_poll_ms
        self.next_at = now + delay

    def _auto(self, now: float) -> None:
        if not self.cfg.auto or self.sw is not None or now - self.last_switch_at < self.cfg.auto_hold_ms:
            return
        if self.fail_ewma > 0.35 and self.profile > 0:
            self.goal = self.profile - 1
            self.fail_ewma = 0.0
            self.policy.up_streak = 0
            return
        avoid = {q for q, t in self.bad.items() if t > now}
        self.goal = self.policy.decide(self.profile, avoid)

    def _auto_power(self, now: float) -> None:
        """Overload protection: lower each side's TX power until the other end hears it at HOT_RSSI."""
        if not self.cfg.auto_power or self.sw is not None or now - self.last_power_at < self.cfg.power_hold_ms:
            return
        t = self.policy.suggest_power("dn", self.dev_power)
        if t is not None:
            self.dev_goal = self.dev_auto_goal = t   # sent as a 'T' frame at the next opportunity
            self.last_power_at = now
            return
        t = self.policy.suggest_power("up", self.ctl_power)
        if t is not None:
            self._apply_ctl_power(now, t)
            self.last_power_at = now

    # ---- timeouts --------------------------------------------------------------------------

    def _on_timeout(self, now: float) -> None:
        inf = self.inflight
        assert inf is not None
        self.timeouts += 1
        tries = inf.tries + 1
        self.event(now, "timeout", what=inf.kind, tries=tries, profile=self.profile)
        if self.state is State.SEARCH:
            if tries >= self.cfg.hunt_tries:
                self._hunt_advance(now)
            else:
                self.retries += 1
                self._tx(now, Frame("H", sid=self.sid, fresh=0 if self.resume_ok else 1), "H", tries)
            return
        if inf.kind == "D" and self.sw is not None and self.sw.stage == "confirm":
            self.sw.confirm_fail += 1
            if self.sw.confirm_fail >= self.cfg.confirm_tries:
                self._switch_failed(now)
                return
        if tries >= self.cfg.max_tries:
            self.event(now, "notice", text="link lost")
            self.fail_ewma = 0.0
            self._enter_search(now, hint=self.profile if self.sw is None else self.sw.frm)
            return
        self.retries += 1
        if inf.kind == "P":
            self._send_switch_request(now, tries)
        elif inf.kind == "T":
            self._send_power_request(now, tries)
        else:
            self._tx(now, self._build_data(), "D", tries)
