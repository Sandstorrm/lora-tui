"""Fuzz the firmware's C++ engine against the Python model: identical inputs, identical outputs.

Feeds random valid, mutated and garbage frames plus timer ticks and CLI output, and requires the
two implementations to emit exactly the same actions and keep exactly the same statistics.  The
C++ side runs under AddressSanitizer/UBSan (tests/harness/build.sh), so this also hunts memory bugs.
"""
import random

import pytest

from bruce_lora.protocol import Frame, encode, pack_quality
from bruce_lora.simdevice import PyDevice
from conftest import _harness_ok
from cppdevice import CppDevice


def cpp_state(d: CppDevice) -> dict:
    s = d.state()
    return dict(linked=int(s["linked"]), profile=int(s["profile"]), switching=int(s["switching"]),
                frames_rx=int(s["rx"]), frames_tx=int(s["tx"]), bad=int(s["bad"]), dup=int(s["dup"]),
                retx=int(s["retx"]), bytes_in=int(s["in"]), bytes_out=int(s["out"]),
                sessions=int(s["sessions"]), reverts=int(s["reverts"]), queued=int(s["queued"]),
                power=int(s["power"]))


def py_state(d: PyDevice) -> dict:
    return dict(linked=int(d.sid != 0), profile=d.profile, switching=int(d.switching or d.apply_after_tx),
                frames_rx=d.stats["frames_rx"], frames_tx=d.stats["frames_tx"], bad=d.stats["bad"],
                dup=d.stats["dup"], retx=d.stats["retx"], bytes_in=d.stats["bytes_in"],
                bytes_out=d.stats["bytes_out"], sessions=d.stats["sessions"], reverts=d.stats["reverts"],
                queued=len(d.out), power=d.power)


def random_frame(rng: random.Random, sid: int, rcv_hint: int = -1) -> bytes:
    kind = rng.choices("HDPTX", weights=[2, 8, 2, 2, 1])[0]
    s = sid if rng.random() < 0.85 else rng.randrange(16)
    if kind == "H":
        return encode(Frame("H", sid=s, ver=rng.choice([1, 1, 1, 2, 0]), fresh=rng.randrange(2)), False)
    if kind == "T":
        return encode(Frame("T", sid=s, power=rng.choice([0, 2, 10, 22, 22, 5])), False)
    if kind == "P":
        return encode(Frame("P", sid=s, target=rng.randrange(16) if rng.random() < 0.3 else rng.randrange(6)), False)
    if kind == "D":
        payload = bytes(rng.randrange(256) for _ in range(rng.choice([0, 0, 1, 5, 40, 100])))
        seq = rcv_hint if rcv_hint >= 0 and rng.random() < 0.6 else rng.randrange(16)
        return encode(Frame("D", sid=s, seq=seq, ack=rng.randrange(16),
                            flags=rng.choice([0, 0, 0, 1, 2, 4, 5, 15]), payload=payload), False)
    return bytes(rng.randrange(0x20, 0x7F) for _ in range(rng.randrange(0, 40)))


def mutate(rng: random.Random, frame: bytes) -> bytes:
    r = rng.random()
    if r < 0.15 and frame:
        i = rng.randrange(len(frame))
        return frame[:i] + bytes([rng.randrange(0x20, 0x7F)]) + frame[i + 1:]
    if r < 0.22:
        return frame[:rng.randrange(len(frame) + 1)]
    if r < 0.26:
        return frame + b"A"
    if r < 0.29 and frame:
        i = rng.randrange(len(frame))
        return frame[:i] + b"\x01" + frame[i + 1:]
    if r < 0.31:
        return b"D5" + b"\\" * rng.randrange(1, 6) + frame[:6]
    return frame


@pytest.mark.parametrize("seed", range(30))
def test_cpp_engine_matches_python_model(seed):
    if not _harness_ok():
        pytest.skip("C++ harness unavailable")
    rng = random.Random(seed)
    py, cpp = PyDevice(), CppDevice()
    try:
        t = 1000
        assert py.begin(t) == cpp.begin(t)
        sid = rng.randrange(1, 16)
        for step in range(500):
            op = rng.choices(["frame", "tick", "txdone", "cli", "want"], weights=[10, 3, 4, 2, 1])[0]
            t += rng.choice([1, 5, 50, 300, 1500, 9000, 45_000]) if rng.random() < 0.7 else 20
            if op == "frame":
                f = mutate(rng, random_frame(rng, py.sid or sid, py.rcv_next if py.sid else -1))
                rssi, snr = rng.randrange(-140, -20), rng.randrange(-20, 12)
                a, b = py.on_frame(t, f, rssi, snr), cpp.on_frame(t, f, rssi, snr)
            elif op == "tick":
                a, b = py.tick(t), cpp.tick(t)
            elif op == "txdone":
                ok = rng.random() < 0.9
                a, b = py.on_tx_done(t, ok), cpp.on_tx_done(t, ok)
            elif op == "cli":
                data = bytes(rng.randrange(256) for _ in range(rng.choice([1, 10, 200, 700])))
                py.cli_write(data)
                cpp.cli_write(data)
                a = b = []
            else:
                w = rng.choice([0, 1, 4, 15])
                py.set_want(w)
                cpp.set_want(w)
                a = b = []
            assert a == b, f"seed {seed} step {step} op {op}\n py: {a}\ncpp: {b}"
            if step % 7 == 0:
                assert py_state(py) == cpp_state(cpp), f"seed {seed} step {step} op {op}"
        assert py_state(py) == cpp_state(cpp)
    finally:
        cpp.close()
