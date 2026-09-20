"""Bruce LoRa link protocol v1 -- wire format, profiles and airtime.

Everything here is pure (no I/O, no clock) so the firmware's C++ engine
(`src/modules/lora/lora_link_engine.cpp` in the Bruce repo) can mirror it 1:1.
See PROTOCOL.md for the prose spec.

Frames are printable ASCII: ``<body><CRC4>`` where CRC4 is CRC-16/CCITT-FALSE of
``body`` in uppercase hex.  Radio layer: RYLR998 ``AT+SEND`` / ``+RCV``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

VERSION = 1
HEX = "0123456789ABCDEF"

# ---- flags (one hex nibble) ------------------------------------------------------------
F_MORE = 0x1   # sender has more queued data after this segment
F_SYNC = 0x2   # (device->controller) SEQ restarts the stream here: adopt it as rcv_next
F_FLUSH = 0x4  # (controller->device) drop your queued/pending output; needs >=1 payload byte

WANT_AUTO = 0xF  # "no preference" value of the device's requested profile
MAX_POWER = 22   # RYLR998 AT+CRFOP range is 0..22 dBm

# ---- profiles --------------------------------------------------------------------------
# The RYLR998 (fw V1.2.3) only accepts SF<=9 @125k, SF<=10 @250k, SF<=11 @500k, and those
# corners all have ~-129 dBm sensitivity, so profile 0 (the proven 9,7,4,4) is the range
# ceiling.  Index 0 is also the rendezvous ("home") profile both ends fall back to.

@dataclass(frozen=True)
class Profile:
    idx: int
    name: str
    sf: int
    bw: int          # RYLR998 code: 7=125k 8=250k 9=500k
    cr: int          # 1..4 = 4/5..4/8
    pre: int
    max_frame: int   # bytes on air, incl. header and CRC

    @property
    def bw_hz(self) -> int:
        return {7: 125_000, 8: 250_000, 9: 500_000}[self.bw]

    @property
    def at_parameter(self) -> str:
        return f"{self.sf},{self.bw},{self.cr},{self.pre}"

    @property
    def snr_limit_db(self) -> float:
        return {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5}[self.sf]

    @property
    def sensitivity_dbm(self) -> float:
        """Rough receiver sensitivity: thermal floor + 6 dB NF + demod SNR limit."""
        return -174 + 10 * math.log10(self.bw_hz) + 6 + self.snr_limit_db


PROFILES = (
    Profile(0, "Range",    9, 7, 4, 4, 128),
    Profile(1, "Long",     8, 7, 4, 4, 128),
    Profile(2, "Standard", 7, 7, 3, 4, 128),
    Profile(3, "Fast",     7, 8, 2, 4, 160),
    Profile(4, "Quick",    7, 9, 1, 4, 192),
    Profile(5, "Turbo",    5, 9, 1, 4, 192),
)
HOME = 0
N_PROFILES = len(PROFILES)


def airtime_ms(profile: int | Profile, frame_len: int) -> float:
    """LoRa time-on-air (explicit header, CRC on) -- validated within ~60 ms on the RYLR998."""
    p = PROFILES[profile] if isinstance(profile, int) else profile
    tsym = (2 ** p.sf) / p.bw_hz
    de = 1 if tsym > 0.016 else 0
    t_pre = (p.pre + (6.25 if p.sf < 7 else 4.25)) * tsym
    num = 8 * frame_len - 4 * p.sf + 28 + 16 + (8 if p.sf < 7 else 0)
    n_sym = 8 + max(math.ceil(num / (4 * (p.sf - 2 * de))) * (p.cr + 4), 0)
    return (t_pre + n_sym * tsym) * 1000.0


T_GUARD_MS = 60          # controller waits this long after a reply before the next frame


def t_wait_ms(profile: int) -> int:
    """How long the controller waits for the device's reply after its own TX finished."""
    return int(150 + 1.2 * airtime_ms(profile, PROFILES[profile].max_frame))


def t_confirm_ms(profile: int) -> int:
    """Device: after switching *to* `profile`, revert if no valid frame arrives within this."""
    return 5 * t_wait_ms(profile) + 3000


T_LOST_MS = 40_000       # device: no valid frame for this long -> drop session, go home


# ---- CRC / escaping --------------------------------------------------------------------

def _hv(c: int) -> int:
    """One strict uppercase hex digit (a byte value) -> 0..15, else ValueError."""
    i = HEX.find(chr(c))
    if i < 0:
        raise ValueError(c)
    return i


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF). crc16(b'123456789') == 0x29B1."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def escape(raw: bytes) -> bytes:
    out = bytearray()
    for b in raw:
        if b == 0x5C:
            out += b"\\\\"
        elif b == 0x0A:
            out += b"\\n"
        elif b == 0x0D:
            out += b"\\r"
        elif 0x20 <= b <= 0x7E:
            out.append(b)
        else:
            out += b"\\x%02X" % b
    return bytes(out)


def escaped_len(b: int) -> int:
    if b in (0x5C, 0x0A, 0x0D):
        return 2
    return 1 if 0x20 <= b <= 0x7E else 4


def unescape(text: bytes) -> Optional[bytes]:
    """Inverse of escape(); None if the text is not a valid escaped stream."""
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c != 0x5C:
            if not 0x20 <= c <= 0x7E:
                return None
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            return None
        e = text[i + 1]
        if e == 0x5C:
            out.append(0x5C); i += 2
        elif e == 0x6E:
            out.append(0x0A); i += 2
        elif e == 0x72:
            out.append(0x0D); i += 2
        elif e == 0x78 and i + 3 < n:
            try:
                out.append(_hv(text[i + 2]) << 4 | _hv(text[i + 3])); i += 4
            except ValueError:
                return None
        else:
            return None
    return bytes(out)


def take_escaped(raw: bytes, budget: int) -> tuple[bytes, int]:
    """Escape as many leading bytes of `raw` as fit in `budget` chars. -> (escaped, n_raw_used)."""
    used = 0
    out = bytearray()
    for i, b in enumerate(raw):
        need = escaped_len(b)
        if used + need > budget:
            return bytes(out), i
        out += escape(bytes((b,)))
        used += need
    return bytes(out), len(raw)


# ---- quality field ---------------------------------------------------------------------

def pack_quality(rssi: int, snr: int) -> bytes:
    rr = max(0, min(255, -rssi))
    ss = max(0, min(255, snr + 64))
    return b"%02X%02X" % (rr, ss)


def unpack_quality(q: bytes) -> tuple[int, int]:
    return -int(q[0:2], 16), int(q[2:4], 16) - 64


# ---- frames ----------------------------------------------------------------------------

@dataclass
class Frame:
    kind: str                    # 'H' 'W' 'D' 'P' 'Q' 'N' 'T' 'U'
    sid: int = 0
    seq: int = 0
    ack: int = 0
    flags: int = 0
    payload: bytes = b""         # raw (unescaped) bytes, kind 'D' only
    ver: int = VERSION           # H, W
    fresh: int = 1               # H: 1 = start a new session even if the sid matches
    res: int = 0                 # W: 1 = the device kept its existing session (resumed)
    cur: int = 0                 # W: device's current profile
    want: int = WANT_AUTO        # W, D(from device): device's requested profile
    target: int = 0              # P, Q
    power: int = 22              # T, U: TX power in dBm (0..22)
    rssi: int = 0                # W, D(from device), Q, N: device's view of the last controller frame
    snr: int = 0


def _h(v: int) -> bytes:
    return HEX[v & 0xF].encode()


def encode(f: Frame, from_device: bool) -> bytes:
    k = f.kind
    if k == "H":
        body = b"H" + _h(f.sid) + _h(f.ver) + _h(f.fresh)
    elif k == "P":
        body = b"P" + _h(f.sid) + _h(f.target)
    elif k == "W":
        body = (b"W" + _h(f.sid) + _h(f.ver) + _h(f.cur) + _h(f.want) + _h(f.res) + b"%02X" % f.power +
                pack_quality(f.rssi, f.snr))
    elif k == "Q":
        body = b"Q" + _h(f.sid) + _h(f.target) + pack_quality(f.rssi, f.snr)
    elif k == "N":
        body = b"N" + _h(f.sid) + pack_quality(f.rssi, f.snr)
    elif k == "T":
        body = b"T" + _h(f.sid) + b"%02X" % f.power
    elif k == "U":
        body = b"U" + _h(f.sid) + b"%02X" % f.power + pack_quality(f.rssi, f.snr)
    elif k == "D":
        body = b"D" + _h(f.sid) + _h(f.seq) + _h(f.ack) + _h(f.flags)
        if from_device:
            body += _h(f.want) + pack_quality(f.rssi, f.snr)
        body += escape(f.payload)
    else:
        raise ValueError(k)
    return body + b"%04X" % crc16(body)


def decode(data: bytes, from_device: bool) -> Optional[Frame]:
    """Parse and verify a frame; None if malformed or the CRC fails."""
    if len(data) < 7 or any(not 0x20 <= c <= 0x7E for c in data):
        return None
    body, crc = data[:-4], data[-4:]
    try:
        if int(crc, 16) != crc16(body) or crc != crc.upper():
            return None
        k = chr(body[0])
        n = len(body)
        if k == "H" and not from_device and n == 4:
            return Frame("H", sid=_hv(body[1]), ver=_hv(body[2]), fresh=_hv(body[3]))
        if k == "P" and not from_device and n == 3:
            return Frame("P", sid=_hv(body[1]), target=_hv(body[2]))
        if k == "W" and from_device and n == 12:
            r, s = unpack_quality(body[8:12])
            pw = _hv(body[6]) << 4 | _hv(body[7])
            if pw > MAX_POWER:
                return None
            return Frame("W", sid=_hv(body[1]), ver=_hv(body[2]), cur=_hv(body[3]), want=_hv(body[4]),
                         res=_hv(body[5]), power=pw, rssi=r, snr=s)
        if k == "Q" and from_device and n == 7:
            r, s = unpack_quality(body[3:7])
            return Frame("Q", sid=_hv(body[1]), target=_hv(body[2]), rssi=r, snr=s)
        if k == "T" and not from_device and n == 4:
            pw = _hv(body[2]) << 4 | _hv(body[3])
            return Frame("T", sid=_hv(body[1]), power=pw) if pw <= MAX_POWER else None
        if k == "U" and from_device and n == 8:
            pw = _hv(body[2]) << 4 | _hv(body[3])
            r, s = unpack_quality(body[4:8])
            return Frame("U", sid=_hv(body[1]), power=pw, rssi=r, snr=s) if pw <= MAX_POWER else None
        if k == "N" and from_device and n == 6:
            r, s = unpack_quality(body[2:6])
            return Frame("N", sid=_hv(body[1]), rssi=r, snr=s)
        if k == "D":
            hdr = 10 if from_device else 5
            if n < hdr:
                return None
            payload = unescape(body[hdr:])
            if payload is None:
                return None
            f = Frame("D", sid=_hv(body[1]), seq=_hv(body[2]), ack=_hv(body[3]), flags=_hv(body[4]),
                      payload=payload)
            if from_device:
                f.want = _hv(body[5])
                f.rssi, f.snr = unpack_quality(body[6:10])
            return f
    except ValueError:
        return None
    return None


def data_budget(profile: int, from_device: bool) -> int:
    """Escaped payload chars that fit in one DATA frame of `profile`."""
    return PROFILES[profile].max_frame - (10 if from_device else 5) - 4
