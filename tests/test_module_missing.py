"""The controller module can be absent at launch or vanish later: the runtime and UI keep going and recover."""
import time

import serial

from bruce_lora.engine import Link, LinkConfig
from bruce_lora.runtime import Runtime
from bruce_lora.tui.app import BruceApp
from bruce_lora.tui.widgets import HeaderBar, SignalPanel
from tests.test_tui import FakeRuntime, SNAP, with_app


class DyingRadio:
    port = "/dev/null"                       # exists, so only the read error can reveal the loss
    power, at_errors = 22, 0

    def __init__(self): self.dead = False
    def poll(self, t=0.0):
        if self.dead:
            raise serial.SerialException("device disconnected")
        time.sleep(t)
    def send_frame(self, d): return True
    def set_profile(self, i): return True
    def set_power(self, d): return True
    def close(self): pass


def wait(cond, s=5.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_runtime_starts_without_a_module_and_connects_when_it_appears():
    snaps, evs, radios = [], [], []
    def connect():
        if not radios:
            raise SystemExit("no serial port found")
        return radios[0]
    rt = Runtime(Link(LinkConfig()), None, on_snapshot=snaps.append, on_events=evs.extend, connect=connect,
                 missing="no serial port found")
    rt.start()
    try:
        assert wait(lambda: snaps and snaps[-1]["state"] == "nomodule")
        assert snaps[-1]["module"] is False and "no serial port" in snaps[-1]["module_error"]
        radios.append(DyingRadio())
        assert wait(lambda: snaps[-1]["module"] is True and snaps[-1]["state"] != "nomodule", 6)
        assert any(e.kind == "module" and e.info["ok"] for e in evs)
    finally:
        rt.stop()


def test_unplugging_mid_run_is_reported_and_recovered():
    r = DyingRadio()
    snaps, evs = [], []
    rt = Runtime(Link(LinkConfig()), r, on_snapshot=snaps.append, on_events=evs.extend,
                 connect=lambda: (_ for _ in ()).throw(SystemExit("gone")))
    rt.start()
    try:
        assert wait(lambda: snaps and snaps[-1]["module"] is True)
        r.dead = True
        assert wait(lambda: snaps[-1]["module"] is False)
        assert snaps[-1]["state"] == "nomodule"
        assert any(e.kind == "module" and not e.info["ok"] for e in evs)
        assert rt.error is None
    finally:
        rt.stop()


def test_ui_shows_no_module_banner_no_bars_and_blocks_sending():
    async def body(app, pilot, q, rt):
        q.put(("snap", dict(SNAP, state="nomodule", module=False, module_error="no serial port found",
                            dn=(None, None), up=(None, None), margin=None)))
        await pilot.pause(0.2)
        assert app.query_one("#nomod").display
        assert "NO MODULE" in app.query_one(HeaderBar).render().plain
        assert "no module" in app.query_one(SignalPanel).render().plain
        assert "▂" not in app.query_one(SignalPanel).render().plain.split("\n")[0]
        for ch in "uptime":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert not any(c[0] == "send" for c in rt.calls)
        q.put(("snap", dict(SNAP, module=True)))
        await pilot.pause(0.2)
        assert not app.query_one("#nomod").display
    with_app(body)
