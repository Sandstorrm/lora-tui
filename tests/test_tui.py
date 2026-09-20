"""Headless UI tests: a stub runtime for exact behaviour, plus one end-to-end run on the demo Core2."""
import asyncio
import queue

from bruce_lora.engine import Link, LinkConfig
from bruce_lora.tui.app import BruceApp, CommandInput
from bruce_lora.tui.demo import DemoRuntime
from bruce_lora.tui.widgets import (NavPad, PowerPanel, RangeSlider, TerminalView, bar, sparkline2, style_line)


class FakeRuntime:
    """Records what the UI asks for; the test feeds snapshots/output through the queue."""
    port = "fake"
    error = None

    def __init__(self):
        self.calls = []

    def start(self): pass
    def stop(self): pass
    def send(self, text): self.calls.append(("send", text))
    def flush(self): self.calls.append(("flush",))
    def set_profile(self, i): self.calls.append(("profile", i))
    def set_auto(self, on): self.calls.append(("auto", on))
    def resync(self): self.calls.append(("resync",))
    def set_power(self, who, dbm): self.calls.append(("power", who, dbm))
    def set_auto_power(self, on): self.calls.append(("autopower", on))


SNAP = dict(state="linked", profile=2, goal=2, auto=True, dn=(-71, 8), up=(-80, 6), margin=31.0, rtt_ms=900.0,
            tx=10, rx=9, bad=0, timeouts=1, retries=1, bytes_in=300, bytes_out=20, link_since=0.0, now=5000.0,
            switching=False, dev_power=22, ctl_power=22, auto_power=True, port="/dev/cu.usbserial-0001", fail=0.0,
            queued=0)


def run(coro):
    return asyncio.run(coro)


def with_app(body, snap=SNAP):
    async def go():
        q, rt = queue.SimpleQueue(), FakeRuntime()
        app = BruceApp(rt, q)
        async with app.run_test(size=(140, 44)) as pilot:
            q.put(("snap", dict(snap)))
            await pilot.pause(0.2)
            await body(app, pilot, q, rt)
    run(go())


def test_typing_sends_the_command_with_a_newline_and_echoes_it():
    async def body(app, pilot, q, rt):
        for ch in "uptime":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert ("send", "uptime\n") in rt.calls
        assert "❯ uptime" in app.query_one(TerminalView).lines
        assert app.query_one(CommandInput).value == ""
    with_app(body)


def test_output_from_the_core2_appears_and_the_prompt_is_a_live_line():
    async def body(app, pilot, q, rt):
        q.put(("out", b"Uptime: 00:01:02\r\n# "))
        await pilot.pause(0.2)
        term = app.query_one(TerminalView)
        assert "Uptime: 00:01:02" in term.lines and term.partial == "# "
        for ch in "free":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert "# free" in term.lines           # the command completes the prompt line, like a real terminal
    with_app(body)


def test_console_has_no_fake_cursor_and_a_click_hands_you_the_real_input():
    async def body(app, pilot, q, rt):
        term = app.query_one(TerminalView)
        q.put(("out", b"Uptime: 1\r\n# "))
        await pilot.pause(0.3)
        assert not hasattr(term, "cursor_on")                       # nothing draws a second, dead caret
        assert "▌" not in str(term.body.render())
        await pilot.press("f2")                                      # focus wanders elsewhere ...
        await pilot.click(TerminalView, offset=(4, 2))               # ... and clicking the console fixes it
        await pilot.pause(0.1)
        assert app.query_one(CommandInput).has_focus
    with_app(body)


def test_a_silent_core2_on_a_healthy_link_is_explained_not_mistaken_for_a_dead_link():
    async def body(app, pilot, q, rt):
        app.busy_hint_s = 0.4
        for ch in "options 1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(1.0)
        notes = [l for l in app.query_one(TerminalView).lines if l.startswith("· no reply yet")]
        assert len(notes) == 1 and "busy" in notes[0]
        await pilot.pause(0.6)                                       # said once, not repeatedly
        assert len([l for l in app.query_one(TerminalView).lines if "no reply yet" in l]) == 1
        q.put(("out", b"Selected option 1\r\n# "))                   # a reply cancels the wait
        for ch in "x":
            await pilot.press(ch)
        await pilot.press("enter")
        q.put(("out", b"ERROR\r\n# "))
        await pilot.pause(1.0)
        assert len([l for l in app.query_one(TerminalView).lines if "no reply yet" in l]) == 1
    with_app(body)


def test_history_and_tab_completion():
    async def body(app, pilot, q, rt):
        for cmd in ("uptime", "free"):
            for ch in cmd:
                await pilot.press(ch)
            await pilot.press("enter")
        await pilot.press("up")
        assert app.query_one(CommandInput).value == "free"
        await pilot.press("up")
        assert app.query_one(CommandInput).value == "uptime"
        await pilot.press("down", "down")
        assert app.query_one(CommandInput).value == ""
        for ch in "upt":
            await pilot.press(ch)
        await pilot.press("tab")
        assert app.query_one(CommandInput).value == "uptime "
    with_app(body)


def test_local_colon_commands_talk_to_the_link_not_the_core2():
    async def body(app, pilot, q, rt):
        for text in (":profile 4", ":profile turbo", ":profile auto", ":power dev 5", ":power auto", ":flush",
                     ":resync"):
            for ch in text:
                await pilot.press(ch)
            await pilot.press("enter")
        await pilot.pause(0.1)
        assert rt.calls == [("profile", 4), ("profile", 5), ("auto", True), ("power", "dev", 5), ("autopower", True),
                            ("flush",), ("resync",)]
    with_app(body)


def test_slider_arrows_pick_a_profile_and_apply_after_a_pause():
    async def body(app, pilot, q, rt):
        await pilot.press("f2")
        assert app.query_one(RangeSlider).has_focus
        await pilot.press("right", "right")
        await pilot.pause(0.8)
        assert rt.calls == [("profile", 4)]                 # from profile 2, two steps faster
        await pilot.press("escape")
        assert app.query_one(CommandInput).has_focus
    with_app(body)


def test_slider_clicking_a_stop_applies_it():
    async def body(app, pilot, q, rt):
        await pilot.click(RangeSlider, offset=(5, 3))       # the leftmost stop = Range
        await pilot.pause(0.2)
        assert rt.calls and rt.calls[-1] == ("profile", 0)
    with_app(body)


def test_power_panel_keys():
    async def body(app, pilot, q, rt):
        await pilot.press("f3")
        await pilot.press("left")                            # first row = Core2
        await pilot.press("down", "left")                    # second row = this radio
        await pilot.press("a")
        await pilot.pause(0.1)
        assert rt.calls == [("power", "dev", 20), ("power", "ctl", 20), ("autopower", False)]
    with_app(body)


def test_nav_pad_and_function_keys_send_nav_commands():
    async def body(app, pilot, q, rt):
        await pilot.click(NavPad, offset=(3, 0))
        await pilot.press("f8", "f7", "f9")
        await pilot.pause(0.1)
        assert [c for c in rt.calls if c[0] == "send"] == [
            ("send", "nav prev\n"), ("send", "nav next\n"), ("send", "nav sel\n"), ("send", "nav esc\n")]
    with_app(body)


def test_ctrl_c_flushes_and_ctrl_l_clears_and_help_opens():
    async def body(app, pilot, q, rt):
        q.put(("out", b"hello\r\n"))
        await pilot.pause(0.1)
        await pilot.press("ctrl+c")
        assert ("flush",) in rt.calls
        await pilot.press("ctrl+l")
        assert not app.query_one(TerminalView).lines
        await pilot.press("f10")
        await pilot.pause(0.1)
        assert app.screen.__class__.__name__ == "HelpScreen"
        await pilot.press("escape")
        assert app.screen.__class__.__name__ != "HelpScreen"
    with_app(body)


def test_link_events_reach_the_packet_log_and_the_terminal():
    from bruce_lora.engine import Event
    from bruce_lora.protocol import Frame

    async def body(app, pilot, q, rt):
        q.put(("events", [Event("state", 1000.0, dict(state="linked", profile=0, resumed=False)),
                          Event("tx", 1100.0, dict(raw=b"x", frame=Frame("H", sid=3), profile=0, retry=0)),
                          Event("rx", 1500.0, dict(raw=b"x", frame=None, rssi=-90, snr=3, profile=0))]))
        await pilot.pause(0.2)
        assert any(l.startswith("· linked") for l in app.query_one(TerminalView).lines)
    with_app(body)


def test_end_to_end_against_the_demo_core2():
    async def go():
        q = queue.SimpleQueue()
        cb = dict(on_output=lambda b: q.put(("out", b)), on_events=lambda e: q.put(("events", e)),
                  on_snapshot=lambda s: q.put(("snap", s)))
        rt = DemoRuntime(Link(LinkConfig(auto=False, target=0)), speed=20.0, walk=False, **cb)
        app = BruceApp(rt, q, demo=True)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(2.0)
            assert app.snap.get("state") == "linked"
            for ch in "uptime":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(2.5)
            assert any(l.startswith("Uptime:") for l in app.query_one(TerminalView).lines)
    run(go())


# ---- pure helpers ---------------------------------------------------------------------------

def test_style_line_colours_prompts_headings_and_entries():
    assert style_line("# uptime").plain == "# uptime"
    assert style_line("WiFi Commands:").spans[0].style.endswith("#7dc8ff") or "bold" in str(style_line("WiFi Commands:").spans[0].style)
    e = style_line("  ir rx <timeout>  - Read an IR signal")
    assert e.plain == "  ir rx <timeout>  - Read an IR signal" and len(e.spans) >= 3


def test_bars_and_sparkline_shapes():
    assert bar(0.5, 10) == "▮▮▮▮▮▯▯▯▯▯" and bar(2, 4) == "▮▮▮▮" and bar(-1, 4) == "▯▯▯▯"
    top, bot = sparkline2([-130, -80, -30], 6)
    assert len(top) == len(bot) == 6
    assert bot[-3] == "▁"                          # weakest sample: lowest block, nothing above it
    assert top[-3] == " "
    assert bot[-1] == "█" and top[-1] == "█"       # strongest sample fills both rows
