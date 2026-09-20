"""TX power control: overload protection for radios side by side, and the safety nets around it."""
import random

from bruce_lora.engine import Link, LinkConfig
from bruce_lora.protocol import HOME
from bruce_lora.sim import Channel, FakeCli, Sim
from test_link import S, connect, make_sim, run_cmd


def dev_power(dev):
    return dev.power if hasattr(dev, "power") else int(dev.state()["power"])


HOT = dict(rssi_up=6, rssi_dn=6, snr_up=10, snr_dn=10, overload_dbm=-20)   # +6 dBm at full power: touching


def test_manual_device_power_change_is_confirmed_and_applied(make_device):
    sim = make_sim(make_device)
    connect(sim)
    sim.link.set_power(sim.t, "dev", 7)
    sim.run_for(20 * S, stop=lambda s: s.link.dev_power == 7)
    sim.run_for(1 * S)                                # the device applies it right after sending the confirmation
    assert sim.link.dev_power == 7 and sim.d_power == 7 and dev_power(sim.dev) == 7
    assert run_cmd(sim, "uptime").startswith(b"Uptime")


def test_controller_power_is_applied_locally(make_device):
    sim = make_sim(make_device)
    connect(sim)
    sim.link.set_power(sim.t, "ctl", 3)
    sim.run_for(5 * S)
    assert sim.c_power == 3 and sim.link.ctl_power == 3


def test_device_returns_to_full_power_when_the_controller_says_hello_again(make_device):
    sim = make_sim(make_device)
    connect(sim)
    sim.link.set_power(sim.t, "dev", 2)
    sim.run_for(20 * S, stop=lambda s: s.d_power == 2)
    sim.run_for(1 * S)
    assert sim.d_power == 2
    sim.restart_controller(Link(LinkConfig(auto=False, target=HOME, auto_power=False), rng=random.Random(4)))
    sim.run_for(30 * S, stop=lambda s: s.linked())
    assert sim.linked() and sim.d_power == 22 and sim.link.dev_power == 22   # WELCOME reported it


def test_device_returns_to_full_power_after_silence(make_device):
    sim = make_sim(make_device)
    connect(sim)
    sim.link.set_power(sim.t, "dev", 2)
    sim.run_for(20 * S, stop=lambda s: s.d_power == 2)
    sim.ch.blackout = lambda t: True
    sim.run_for(50 * S)
    assert sim.d_power == 22


def test_auto_power_tames_radios_that_are_touching(make_device):
    sim = make_sim(make_device, cfg=LinkConfig(auto=False, target=HOME), channel=Channel(**HOT), seed=7)
    connect(sim, 120 * S)
    sim.run_for(90 * S)
    assert sim.d_power <= 6 and sim.c_power <= 6, (sim.d_power, sim.c_power)
    assert sim.linked()
    # once tamed, the link is as clean as a normal one
    r0, t0 = sim.link.retries, sim.link.timeouts
    for cmd in ("free", "uptime", "free", "uptime"):
        assert run_cmd(sim, cmd, 30 * S).endswith(b"# ")
    assert sim.link.retries - r0 <= 1


def test_overload_really_hurts_without_power_control(make_device):
    sim = make_sim(make_device, cfg=LinkConfig(auto=False, target=HOME, auto_power=False),
                   channel=Channel(**HOT), seed=7)
    connect(sim, 300 * S)
    sim.run_for(30 * S)
    r0 = sim.link.retries
    for cmd in ("free", "uptime", "free", "uptime"):
        run_cmd(sim, cmd, 120 * S)
    assert sim.link.retries - r0 >= 3


def test_weak_links_keep_full_power(make_device):
    ch = Channel(rssi_up=-100, rssi_dn=-100, snr_up=-3, snr_dn=-3, overload_dbm=-20)
    sim = make_sim(make_device, channel=ch)
    connect(sim)
    sim.run_for(90 * S)
    assert sim.d_power == 22 and sim.c_power == 22


def test_power_comes_back_up_when_the_link_weakens(make_device):
    ch = Channel(**HOT)
    sim = make_sim(make_device, cfg=LinkConfig(auto=False, target=HOME), channel=ch, seed=3)
    connect(sim, 120 * S)
    sim.run_for(90 * S)
    assert sim.d_power <= 6
    ch.rssi_up = ch.rssi_dn = -105                      # walked away
    ch.snr_up = ch.snr_dn = -2
    sim.run_for(240 * S)
    assert sim.linked()
    assert sim.d_power >= 20 and sim.c_power >= 20, (sim.d_power, sim.c_power)


def test_power_is_put_back_right_after_a_reconnect(make_device):
    """The Core2 resets to full power on HELLO (safety); a resumed session must re-lower it immediately,
    not wait for the policy again -- otherwise every reconnect re-overloads radios that are side by side."""
    sim = make_sim(make_device, cfg=LinkConfig(auto=False, target=HOME), channel=Channel(**HOT), seed=7)
    connect(sim, 120 * S)
    sim.run_for(60 * S)
    assert sim.d_power <= 6, sim.d_power
    lowered = sim.d_power
    sim.link.resync(sim.t)                                   # drop to searching and resume the same session
    sim.run_for(30 * S, stop=lambda s: s.linked())
    assert sim.linked()
    sim.run_for(4 * S)                                       # a couple of exchanges, not a whole policy cycle
    assert sim.d_power <= lowered + 2, (sim.d_power, lowered)
