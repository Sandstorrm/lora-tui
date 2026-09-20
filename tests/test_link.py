"""End-to-end link behaviour: the controller engine vs the Core2 engine (Python model AND the
firmware's real C++ engine, via the `make_device` fixture) over a simulated radio."""
import math
import random

import pytest

from bruce_lora.engine import Link, LinkConfig
from bruce_lora.protocol import HOME, PROFILES, t_confirm_ms
from bruce_lora.sim import Channel, FakeCli, Sim

S = 1000.0


def make_sim(make_device, cfg=None, channel=None, seed=1):
    link = Link(cfg or LinkConfig(auto=False, target=HOME), rng=random.Random(seed))
    return Sim(link, make_device(), channel or Channel(), seed=seed, cli=FakeCli())


def connect(sim, limit=60 * S):
    sim.run(sim.t + limit, stop=lambda s: s.linked())
    assert sim.linked(), "never linked"


def run_cmd(sim, text, timeout=120 * S):
    sim.out.clear()
    sim.type(text + "\n")
    sim.run_for(timeout, stop=lambda s: s.out.endswith(b"# "))
    return bytes(sim.out)


def dev_profile(dev):
    return dev.profile if hasattr(dev, "profile") else int(dev.state()["profile"])


def dev_linked(dev):
    return dev.sid != 0 if hasattr(dev, "sid") else dev.state()["linked"] == "1"


def snr_for(rssi):
    """Roughly what a RYLR998 reports: RSSI above the noise floor, clipped near +10 dB."""
    return lambda p: min(10, rssi + 117 - 10 * math.log10(PROFILES[p].bw_hz / 125_000))


# ---- basics ------------------------------------------------------------------------------

def test_connects_and_runs_a_command(make_device):
    sim = make_sim(make_device)
    connect(sim)
    assert sim.t < 3 * S
    assert run_cmd(sim, "uptime").startswith(b"Uptime: ")
    assert run_cmd(sim, "bogus") == b"ERROR: Unknown command 'bogus'\r\n# "


def test_idle_link_keeps_polling_cheaply(make_device):
    sim = make_sim(make_device)
    connect(sim)
    t0, sent0 = sim.t, sim.frames_sent["c"]
    sim.run_for(60 * S)
    per_min = sim.frames_sent["c"] - sent0
    assert sim.linked()
    assert 8 <= per_min <= 40          # idle keepalive, not a firehose


# ---- reliability ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(6))
def test_commands_arrive_exactly_once_in_order_under_loss(make_device, seed):
    sim = make_sim(make_device, channel=Channel(loss=0.2, corrupt=0.1, dup=0.1), seed=seed)
    connect(sim, 120 * S)
    cmds = [f"cmd{i}" for i in range(40)]
    expected_in = "".join(c + "\n" for c in cmds).encode()
    expected_out = b"".join(FakeCli().run(c) for c in cmds)
    sim.out.clear()
    sim.type("".join(c + "\n" for c in cmds))
    sim.run_for(30 * 60 * S, stop=lambda s: len(s.out) >= len(expected_out))
    assert bytes(sim.cli_in) == expected_in
    assert bytes(sim.out) == expected_out
    assert sim.link.retries > 0        # the channel really was lossy


def test_large_binary_output_survives_a_bad_channel(make_device):
    sim = make_sim(make_device, cfg=LinkConfig(auto=False, target=4),
                   channel=Channel(loss=0.15, corrupt=0.05, dup=0.05), seed=3)
    connect(sim, 120 * S)
    blob = bytes(range(256)) * 12 + b"\\\r\n" * 50
    sim.dev.cli_write(blob)
    sim.run_for(30 * 60 * S, stop=lambda s: len(s.out) >= len(blob))
    assert bytes(sim.out) == blob


def test_flush_drops_stale_output_and_the_stream_resyncs(make_device):
    sim = make_sim(make_device)
    connect(sim)
    sim.dev.cli_write(b"A" * 4000)
    sim.run_for(6 * S)
    got = len(sim.out)
    assert 0 < got < 4000
    sim.link.flush(sim.t)
    sim.run_for(30 * S, stop=lambda s: s.out.endswith(b"# "))
    assert sim.out.endswith(b"# ")                       # the flush's newline re-drew the prompt
    assert len(sim.out) < 4000
    tail_start = len(sim.out)
    assert run_cmd(sim, "uptime").startswith(b"Uptime: ")  # stream still healthy afterwards
    sim.run_for(20 * S)
    assert b"A" not in sim.out                             # stale output never resurfaces


# ---- session / recovery --------------------------------------------------------------------

def test_device_reboot_is_noticed_and_relinked(make_device):
    sim = make_sim(make_device)
    connect(sim)
    assert run_cmd(sim, "uptime").startswith(b"Uptime")
    sim.reboot_device(make_device())
    assert run_cmd(sim, "free").startswith(b"Total heap")   # relinked transparently, command still ran


def test_recovers_from_a_long_blackout(make_device):
    win = (10 * S, 40 * S)
    sim = make_sim(make_device, channel=Channel(blackout=lambda t: win[0] <= t < win[1]))
    connect(sim)
    sim.run(win[1] + 1)
    sim.run_for(60 * S, stop=lambda s: s.linked())
    assert sim.linked()
    assert run_cmd(sim, "uptime").startswith(b"Uptime")


def test_device_goes_home_and_forgets_the_session_after_silence(make_device):
    sim = make_sim(make_device)
    connect(sim)
    sim.link.set_profile(sim.t, 3)
    sim.run_for(30 * S, stop=lambda s: dev_profile(s.dev) == 3 and s.link.sw is None)
    assert dev_profile(sim.dev) == 3
    sim.ch.blackout = lambda t: True                      # the controller vanishes
    sim.run_for(45 * S)
    assert dev_profile(sim.dev) == HOME and not dev_linked(sim.dev)


# ---- profiles ------------------------------------------------------------------------------

def test_manual_profile_switch_moves_both_ends_and_speeds_things_up(make_device):
    sim = make_sim(make_device)
    connect(sim)
    t0 = sim.t
    run_cmd(sim, "help")
    slow = sim.t - t0
    sim.link.set_profile(sim.t, 4)
    sim.run_for(30 * S, stop=lambda s: s.link.profile == 4 and s.link.sw is None)
    assert sim.link.profile == 4 and dev_profile(sim.dev) == 4
    t1 = sim.t
    out = run_cmd(sim, "help")
    fast = sim.t - t1
    assert out.startswith(b"Bruce vdev") and out.endswith(b"# ")
    assert fast * 4 < slow


def test_switch_survives_a_lost_confirmation(make_device):
    dropped = []

    def drop_first_q(direction, frame, t, prof):
        if direction == "d>c" and frame.startswith(b"Q") and not dropped:
            dropped.append(t)
            return True
        return False

    sim = make_sim(make_device, channel=Channel(filter=drop_first_q))
    connect(sim)
    sim.link.set_profile(sim.t, 3)
    sim.run_for(60 * S, stop=lambda s: s.link.profile == 3 and s.link.sw is None and s.linked())
    assert dropped and sim.link.profile == 3 and dev_profile(sim.dev) == 3
    assert run_cmd(sim, "uptime").startswith(b"Uptime")


def test_switch_to_an_unreachable_profile_backs_out_cleanly(make_device):
    sim = make_sim(make_device, channel=Channel(filter=lambda d, f, t, prof: prof == 4))
    connect(sim)
    sim.link.set_profile(sim.t, 4)
    sim.run_for(90 * S, stop=lambda s: 4 in s.link.bad and s.link.sw is None)
    assert 4 in sim.link.bad
    sim.run_for(t_confirm_ms(4) + 5 * S)                  # let the device time out and come back too
    assert sim.link.profile == HOME and dev_profile(sim.dev) == HOME
    assert sim.linked()
    assert run_cmd(sim, "uptime").startswith(b"Uptime")


def test_hunt_finds_a_device_left_on_another_profile(make_device):
    sim = make_sim(make_device)
    connect(sim)
    sim.link.set_profile(sim.t, 3)
    sim.run_for(30 * S, stop=lambda s: s.link.profile == 3 and s.link.sw is None)
    t0 = sim.t
    sim.restart_controller(Link(LinkConfig(auto=False, target=3), rng=random.Random(9)))
    sim.run_for(60 * S, stop=lambda s: s.linked())
    assert sim.linked() and sim.link.profile == 3
    assert sim.t - t0 < 20 * S
    assert run_cmd(sim, "uptime").startswith(b"Uptime")


def test_core2_slider_request_is_followed(make_device):
    sim = make_sim(make_device, cfg=LinkConfig(auto=True))
    connect(sim)
    sim.dev.set_want(4)
    sim.run_for(60 * S, stop=lambda s: s.link.profile == 4 and s.link.sw is None)
    assert sim.link.profile == 4 and dev_profile(sim.dev) == 4 and not sim.link.cfg.auto


# ---- auto range/speed ----------------------------------------------------------------------

def test_auto_climbs_when_the_link_is_strong(make_device):
    sim = make_sim(make_device, cfg=LinkConfig(auto=True),
                   channel=Channel(rssi_up=-45, rssi_dn=-45, snr_up=snr_for(-45), snr_dn=snr_for(-45),
                                   enforce_sensitivity=True))
    connect(sim)
    sim.run_for(120 * S, stop=lambda s: s.link.profile == len(PROFILES) - 1 and s.link.sw is None)
    assert sim.link.profile == len(PROFILES) - 1
    assert dev_profile(sim.dev) == sim.link.profile


def test_auto_steps_down_gracefully_as_the_signal_fades(make_device):
    ch = Channel(enforce_sensitivity=True)
    sim = make_sim(make_device, cfg=LinkConfig(auto=True), channel=ch, seed=2)
    seen, lost = [], 0
    for rssi in (-45, -75, -95, -105, -112, -118, -124):
        ch.rssi_up = ch.rssi_dn = rssi
        ch.snr_up = ch.snr_dn = snr_for(rssi)
        sim.run_for(150 * S)
        assert sim.linked(), f"lost the link at {rssi} dBm"
        seen.append(sim.link.profile)
        assert dev_profile(sim.dev) == sim.link.profile
    assert seen == sorted(seen, reverse=True), seen        # never speeds up while fading
    assert seen[0] >= 4 and seen[-1] <= 1, seen
    sim.dev.cli_write(b"still here\r\n# ")
    sim.run_for(60 * S, stop=lambda s: s.out.endswith(b"# "))
    assert b"still here" in sim.out


def test_auto_recovers_from_a_sudden_drop(make_device):
    ch = Channel(enforce_sensitivity=True, rssi_up=-45, rssi_dn=-45, snr_up=snr_for(-45), snr_dn=snr_for(-45))
    sim = make_sim(make_device, cfg=LinkConfig(auto=True), channel=ch, seed=5)
    connect(sim)
    sim.run_for(120 * S, stop=lambda s: s.link.profile >= 4 and s.link.sw is None)
    assert sim.link.profile >= 4
    ch.rssi_up = ch.rssi_dn = -122                         # walked behind a wall: only profile 0 can work
    ch.snr_up = ch.snr_dn = snr_for(-122)
    sim.run_for(180 * S)
    assert sim.linked() and sim.link.profile == 0
    assert run_cmd(sim, "uptime").startswith(b"Uptime")
