import math

import pytest

from bruce_lora.protocol import (PROFILES, Frame, airtime_ms, crc16, data_budget, decode, encode, escape,
                                 t_confirm_ms, t_wait_ms, take_escaped, unescape)


def test_crc_check_value():
    assert crc16(b"123456789") == 0x29B1


def test_escape_roundtrip_all_bytes_and_printable():
    raw = bytes(range(256)) * 3
    e = escape(raw)
    assert all(0x20 <= c <= 0x7E for c in e)
    assert unescape(e) == raw


@pytest.mark.parametrize("bad", [b"\\", b"\\x4", b"\\xZZ", b"\\q", b"\\x4g", b"a\x01b", b"caf\xc3\xa9", b"\\x4a"])
def test_unescape_rejects_malformed(bad):
    # lowercase hex digits are not produced by escape(), so they are rejected too
    assert unescape(bad) is None


def test_take_escaped_respects_budget_and_never_splits_an_escape():
    raw = b"a\x00b\nc" * 40
    for budget in range(4, 60):
        esc, used = take_escaped(raw, budget)
        assert len(esc) <= budget
        assert unescape(esc) == raw[:used]
        # one more byte would not have fit
        nxt = escape(raw[used:used + 1])
        assert used == len(raw) or len(esc) + len(nxt) > budget


FRAMES = [
    (Frame("H", sid=5), False),
    (Frame("P", sid=15, target=5), False),
    (Frame("W", sid=1, cur=3, want=0xF, res=1, power=17, rssi=-80, snr=7), True),
    (Frame("Q", sid=9, target=2, rssi=-120, snr=-15), True),
    (Frame("N", sid=9, rssi=-4, snr=10), True),
    (Frame("T", sid=4, power=0), False),
    (Frame("T", sid=4, power=22), False),
    (Frame("U", sid=4, power=13, rssi=-61, snr=9), True),
    (Frame("D", sid=2, seq=15, ack=0, flags=7, payload=b"\x00\xff\r\n\\ ~"), False),
    (Frame("D", sid=2, seq=1, ack=2, flags=3, payload=b"hello", want=4, rssi=-99, snr=-3), True),
]


@pytest.mark.parametrize("frame,dev", FRAMES)
def test_frame_roundtrip_and_corruption_detected(frame, dev):
    wire = encode(frame, dev)
    assert decode(wire, dev) == frame
    assert len(wire) <= 200
    for i in range(len(wire)):                      # any single altered character is caught
        bad = bytearray(wire)
        bad[i] = ord("z") if bad[i] != ord("z") else ord("y")
        assert decode(bytes(bad), dev) is None, i


def test_power_frames_reject_out_of_range():
    body = b"T417"                                   # 0x17 = 23 dBm: not a valid RYLR998 power
    from bruce_lora.protocol import crc16
    assert decode(body + b"%04X" % crc16(body), False) is None


def test_decode_is_direction_aware():
    assert decode(encode(Frame("H", sid=3), False), True) is None
    assert decode(encode(Frame("W", sid=3), True), False) is None


def test_wire_sample_is_stable():
    """Golden frame: the C++ engine must produce/accept exactly this."""
    f = Frame("D", sid=5, seq=3, ack=9, flags=3, payload=b"uptime\n", want=15, rssi=-77, snr=7)
    assert encode(f, True) == b"D5393F4D47uptime\\nB724"


def test_data_budget_fits_max_frame():
    for p in PROFILES:
        payload = b"\x01" * (data_budget(p.idx, True) // 4)
        assert len(encode(Frame("D", payload=payload, want=1), True)) <= p.max_frame


def test_airtime_matches_measured_module_timings():
    """+OK on the RYLR998 arrives after the transmission; measured on hardware (ms, 128-byte payload)."""
    measured = {0: 1077, 1: 613, 2: 323, 3: 156, 4: 77, 5: 43}
    for idx, ms in measured.items():
        est = airtime_ms(idx, 128)
        assert est < ms < est + 70, (idx, est, ms)


def test_profiles_get_faster_and_less_sensitive():
    air = [airtime_ms(p.idx, 128) for p in PROFILES]
    sens = [p.sensitivity_dbm for p in PROFILES]
    assert air == sorted(air, reverse=True)
    assert sens == sorted(sens)
    assert math.isclose(PROFILES[0].sensitivity_dbm, -129.5, abs_tol=0.1)


def test_timeouts_scale_with_airtime():
    assert t_wait_ms(0) > 2 * t_wait_ms(3) > 0
    assert all(t_confirm_ms(p) > 4 * t_wait_ms(p) for p in range(len(PROFILES)))
