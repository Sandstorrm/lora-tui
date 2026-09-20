"""Pure-Python model of the Core2's link engine -- a line-by-line mirror of
`src/modules/lora/lora_link_engine.cpp` in the Bruce firmware.

Used for `--demo` (a simulated Core2) and in tests, where it is checked against the real C++
engine (tests/harness) so the two cannot drift apart.

Every method returns a list of actions: ("send", frame), ("profile", idx), ("rxpush", bytes).
"""
from __future__ import annotations

from . import protocol as pr
from .protocol import (F_FLUSH, F_MORE, F_SYNC, HOME, N_PROFILES, WANT_AUTO, HEX, crc16, escape, pack_quality,
                       t_confirm_ms, unescape)

MAX_FRAME = 200


class PyDevice:
    def __init__(self) -> None:
        self.out = bytearray()          # CLI output waiting to be sent (device -> controller)
        self.rx_cap = 4096
        self.stats = dict(frames_rx=0, frames_tx=0, bad=0, dup=0, retx=0, bytes_in=0, bytes_out=0,
                          sessions=0, reverts=0)
        self.sid = 0
        self.profile = HOME
        self.want = WANT_AUTO
        self.rssi, self.snr = -255, 0
        self.last_rx = 0.0
        self.snd_seq = self.rcv_next = 0
        self.pend = False
        self.pend_sync = self.sync_needed = self.pend_sent = False
        self.pend_buf = b""
        self.apply_after_tx = self.switching = False
        self.power = 22
        self.default_power = 22
        self.apply_power_after_tx = False
        self.sw_power = 22
        self.sw_target = self.sw_from = 0
        self.confirm_deadline = 0.0
        self.acts: list = []

    # ---- host side -------------------------------------------------------------------------
    def cli_write(self, data: bytes) -> None:
        self.out += data

    def set_want(self, w: int) -> None:
        self.want = w

    def set_default_power(self, w: int) -> None:
        self.default_power = w

    def _take(self) -> list:
        a, self.acts = self.acts, []
        return a

    # ---- engine ----------------------------------------------------------------------------
    def begin(self, now: float) -> list:
        self.last_rx = now
        self.profile = HOME
        self.switching = self.apply_after_tx = False
        self.apply_power_after_tx = False
        self.power = self.default_power
        self._reset_session()
        return self._take()

    def _restore_power(self) -> None:
        self.apply_power_after_tx = False
        if self.power != self.default_power:
            self.power = self.default_power
            self.acts.append(("power", self.power))

    def _reset_session(self) -> None:
        self.sid = 0
        self.snd_seq = self.rcv_next = 0
        self.pend = self.pend_sync = self.sync_needed = self.pend_sent = False
        self.out.clear()

    def _go_home(self) -> None:
        self.switching = self.apply_after_tx = False
        self.profile = HOME
        self.acts.append(("profile", HOME))
        self.stats["reverts"] += 1

    def on_tx_done(self, now: float, ok: bool = True) -> list:
        if self.apply_power_after_tx:
            self.apply_power_after_tx = False
            if ok:
                self.power = self.sw_power
                self.acts.append(("power", self.power))
        if self.apply_after_tx:
            self.apply_after_tx = False
            if ok:
                self.switching = True
                self.profile = self.sw_target
                self.acts.append(("profile", self.profile))
                self.confirm_deadline = now + t_confirm_ms(self.profile)
        return self._take()

    def tick(self, now: float) -> list:
        if self.switching and now - self.confirm_deadline > 0:
            self.switching = False
            self.profile = self.sw_from
            self.acts.append(("profile", self.profile))
            self.last_rx = now
        if not self.apply_after_tx and now - self.last_rx > pr.T_LOST_MS:
            if self.sid:
                self._reset_session()
            if self.profile != HOME:
                self._go_home()
            self._restore_power()
            self.last_rx = now
        return self._take()

    def _reply(self, body: bytes) -> None:
        self.acts.append(("send", body + b"%04X" % crc16(body)))
        self.stats["frames_tx"] += 1

    def _reply_data(self) -> None:
        budget = pr.PROFILES[self.profile].max_frame - 10 - 4
        if not self.pend and self.out:
            raw = bytes(self.out[:budget])
            esc, used = pr.take_escaped(raw, budget)
            if used > 0:
                del self.out[:used]
                self.stats["bytes_out"] += used
                self.pend_buf = esc
                self.pend, self.pend_sent = True, False
                self.pend_sync, self.sync_needed = self.sync_needed, False
        flags = (F_MORE if self.out else 0) | (F_SYNC if self.pend and self.pend_sync else 0)
        body = (b"D" + HEX[self.sid].encode() + HEX[self.snd_seq].encode() + HEX[self.rcv_next].encode() +
                HEX[flags].encode() + HEX[self.want].encode() + pack_quality(self.rssi, self.snr))
        if self.pend:
            body += self.pend_buf
            if self.pend_sent:
                self.stats["retx"] += 1
            self.pend_sent = True
        self._reply(body)

    def _handle_data(self, b: bytes) -> None:
        try:
            seq, ack, flags = HEX.index(chr(b[2])), HEX.index(chr(b[3])), HEX.index(chr(b[4]))
        except ValueError:
            self.stats["bad"] += 1
            return
        raw = unescape(b[5:])
        if raw is None or len(raw) > MAX_FRAME:
            self.stats["bad"] += 1
            return
        if self.pend and ack == (self.snd_seq + 1) & 15:
            self.pend = False
            self.snd_seq = (self.snd_seq + 1) & 15
        if raw:
            if seq == self.rcv_next:
                if self.rx_cap >= len(raw):
                    self.acts.append(("rxpush", raw))
                    self.stats["bytes_in"] += len(raw)
                    self.rcv_next = (self.rcv_next + 1) & 15
                    if flags & F_FLUSH:
                        self.out.clear()
                        self.pend = False
                        self.snd_seq = (self.snd_seq + 1) & 15
                        self.sync_needed = True
            elif seq == (self.rcv_next + 15) & 15:
                self.stats["dup"] += 1
        self._reply_data()

    def on_frame(self, now: float, d: bytes, rssi: int, snr: int) -> list:
        st = self.stats
        if len(d) < 7 or len(d) > MAX_FRAME or any(not 0x20 <= c <= 0x7E for c in d):
            st["bad"] += 1
            return self._take()
        body, crc = d[:-4], d[-4:]
        if any(chr(c) not in HEX for c in crc) or int(crc, 16) != crc16(body):
            st["bad"] += 1
            return self._take()
        kind = chr(body[0])
        if chr(body[1]) not in HEX:
            st["bad"] += 1
            return self._take()
        sid = HEX.index(chr(body[1]))
        st["frames_rx"] += 1
        self.last_rx = now
        self.rssi, self.snr = rssi, snr
        if self.switching:
            self.switching = False
        q = pack_quality(self.rssi, self.snr)
        n = len(body)

        def nosession() -> None:
            self._reply(b"N" + HEX[sid].encode() + q)

        if kind == "H" and n == 4:
            ver, fresh = HEX.find(chr(body[2])), HEX.find(chr(body[3]))
            if sid == 0 or ver != pr.VERSION or fresh < 0:
                return self._take()
            self._restore_power()
            resumed = 1
            if fresh or sid != self.sid:
                self._reset_session()
                self.sid = sid
                st["sessions"] += 1
                resumed = 0
            self._reply(b"W" + HEX[self.sid].encode() + HEX[pr.VERSION].encode() + HEX[self.profile].encode() +
                        HEX[self.want].encode() + HEX[resumed].encode() + b"%02X" % self.power + q)
        elif kind == "D" and n >= 5:
            if sid != self.sid or self.sid == 0:
                nosession()
            else:
                self._handle_data(body)
        elif kind == "T" and n == 4:
            if sid != self.sid or self.sid == 0:
                nosession()
            else:
                hi, lo = HEX.find(chr(body[2])), HEX.find(chr(body[3]))
                if hi < 0 or lo < 0 or (hi << 4 | lo) > pr.MAX_POWER:
                    return self._take()
                pw = hi << 4 | lo
                self._reply(b"U" + HEX[self.sid].encode() + b"%02X" % pw + q)
                if pw != self.power:
                    self.apply_power_after_tx = True
                    self.sw_power = pw
        elif kind == "P" and n == 3:
            if sid != self.sid or self.sid == 0:
                nosession()
            elif chr(body[2]) in HEX and HEX.index(chr(body[2])) < N_PROFILES:
                target = HEX.index(chr(body[2]))
                if target != self.profile:
                    self.apply_after_tx = True
                    self.sw_target, self.sw_from = target, self.profile
                self._reply(b"Q" + HEX[self.sid].encode() + HEX[target].encode() + q)
        else:
            st["bad"] += 1
        return self._take()
