"""Screen mode: the Core2's menus/apps as a keyboard-driven view."""
import asyncio
import json
import queue

from bruce_lora.engine import Link, LinkConfig
from bruce_lora.tui import screenview
from bruce_lora.tui.app import BruceApp, CommandInput
from bruce_lora.tui.demo import DemoRuntime
from bruce_lora.tui.screenview import REFRESH_STOPS, RefreshSlider, ScreenView, parse_screen
from bruce_lora.tui.widgets import TerminalView
from test_tui import SNAP, with_app

MAIN = dict(w=320, h=220, mode="menu", type="main", title="Main Menu", sel=0,
            options=["WiFi", "BLE", "RF", "NRF24", "LoRa", "FM", "IR", "Ethernet", "GPS", "RFID", "Files",
                     "JS Interpreter", "Clock", "Others", "Config"])
BLE = dict(w=320, h=220, mode="menu", type="sub", title="Bluetooth", sel=0,
           options=["Media Cmds", "BLE Scan", "iBeacon", "Bad BLE", "BLE Keyboard", "BLE Spam", "Main Menu"])
IBEACON = dict(w=320, h=220, mode="app", title="Bluetooth",
               lines=["17:51:25  CHG", "IBEACON", "UUID:e4c159a0-8c82-11e6-bdf4-0800200c9a66",
                      "Press Any key to STOP.", "PREV  SEL  NEXT"])


def reply(q, d):
    q.put(("out", (json.dumps(d, separators=(",", ":")) + "\r\n# ").encode()))


def sent(rt):
    return [c[1].strip() for c in rt.calls if c[0] == "send"]


def test_parse_screen_accepts_reports_and_ignores_everything_else():
    assert parse_screen(json.dumps(MAIN))["mode"] == "menu"
    assert parse_screen(json.dumps(IBEACON))["lines"][1] == "IBEACON"
    assert parse_screen("# " + json.dumps(MAIN))["mode"] == "menu"        # the last prompt sits in front of the reply
    for junk in ("Uptime: 00:01:02", "{not json}", '{"mode":"other"}', "# ", "", "x " + json.dumps(MAIN)):
        assert parse_screen(junk) is None


def test_f5_swaps_the_terminal_for_the_screen_and_asks_the_core2_what_is_showing():
    async def body(app, pilot, q, rt):
        await pilot.press("f5")
        await pilot.pause(0.2)
        sv, term = app.query_one(ScreenView), app.query_one(TerminalView)
        assert sv.display and not term.display and sv.has_focus
        assert sent(rt) == ["screen view"]
        reply(q, MAIN)
        await pilot.pause(0.3)
        assert sv.mode == "menu" and sv.options[1] == "BLE"
        assert not any("mode" in l for l in term.lines)             # the report never lands in the scrollback
        await pilot.press("f5")                                       # and back again
        await pilot.pause(0.1)
        assert term.display and not sv.display and app.query_one(CommandInput).has_focus
    with_app(body)


def test_arrows_move_a_local_cursor_and_enter_opens_with_a_single_command():
    async def body(app, pilot, q, rt):
        await pilot.press("f5")
        reply(q, MAIN)
        await pilot.pause(0.3)
        sv = app.query_one(ScreenView)
        await pilot.press("down", "down", "up")
        assert sv.sel == 1 and sent(rt) == ["screen view"]              # moving costs no radio traffic
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert sent(rt)[-1] == "screen view open 1"
        reply(q, BLE)
        await pilot.pause(0.3)
        assert sv.path == ["BLE"] and sv.title == "Bluetooth" and sv.options[2] == "iBeacon" and sv.sel == 0
        assert "Main › BLE" in sv.border_title
        await pilot.press("down", "down", "enter")
        await pilot.pause(0.1)
        assert sent(rt)[-1] == "screen view open 2"
        reply(q, IBEACON)
        await pilot.pause(0.3)
        assert sv.mode == "app" and sv.path == ["BLE", "iBeacon"] and "IBEACON" in sv.lines
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert sent(rt)[-1] == "screen view back"
        reply(q, MAIN)                                                   # the Core2 drops back to the main menu
        await pilot.pause(0.3)
        assert sv.mode == "menu" and sv.path == [] and sv.title == "Main Menu"
    with_app(body)


def test_only_one_request_is_in_flight_and_a_missing_reply_is_reported():
    async def body(app, pilot, q, rt):
        screenview.REQUEST_TIMEOUT_S = 0.6
        await pilot.press("f5")
        reply(q, MAIN)
        await pilot.pause(0.3)
        await pilot.press("enter", "enter", "enter")
        await pilot.pause(0.1)
        assert sent(rt).count("screen view open 0") == 1                # the second and third were ignored
        await pilot.pause(1.4)                                           # no reply ever comes
        sv = app.query_one(ScreenView)
        assert sv.error == "no reply from the Core2"
        await pilot.press("enter")                                       # and it can be tried again
        await pilot.pause(0.1)
        assert sent(rt).count("screen view open 0") == 2
    with_app(body)


def test_the_command_line_gives_way_to_the_refresh_slider_in_screen_mode_and_back():
    async def body(app, pilot, q, rt):
        cmd, rate = app.query_one(CommandInput), app.query_one(RefreshSlider)
        assert cmd.display and not rate.display
        await pilot.press("f5")
        await pilot.pause(0.2)
        assert rate.display and not cmd.display
        await pilot.press("f5")
        await pilot.pause(0.2)
        assert cmd.display and not rate.display and cmd.has_focus
    with_app(body)


def test_refresh_slider_changes_how_often_the_screen_looks_and_can_turn_it_off():
    async def body(app, pilot, q, rt):
        sv, rate = app.query_one(ScreenView), app.query_one(RefreshSlider)
        assert sv.refresh_s == 5.0
        await pilot.press("f5")
        reply(q, MAIN)
        await pilot.pause(0.3)
        await pilot.press("tab")                                          # Tab hands the keyboard to the slider
        assert rate.has_focus
        await pilot.press("right")
        assert sv.refresh_s == 10.0
        await pilot.press("home")
        assert sv.refresh_s is None                                       # off
        n = len(sent(rt))
        sv.refresh_s = None
        await pilot.pause(1.5)
        assert len(sent(rt)) == n                                         # off means off, menus included
        await pilot.press("right", "right")                               # 1 s, 2 s
        assert sv.refresh_s == 2.0
        sv.refresh_s = 0.4                                                # (fast for the test)
        reply(q, MAIN)
        await pilot.pause(0.3)
        await pilot.pause(1.4)
        assert len(sent(rt)) > n and sent(rt)[-1] == "screen view"        # a menu is refreshed too
        await pilot.press("escape")
        assert sv.has_focus
        await pilot.press("right_square_bracket")                         # ] works from the screen as well
        assert rate.idx == 3 and sv.refresh_s == 5.0                      # from "2 s" one step up: "5 s"
    with_app(body)


def test_clicking_the_slider_picks_a_stop():
    async def body(app, pilot, q, rt):
        await pilot.press("f5")
        await pilot.pause(0.2)
        rate, sv = app.query_one(RefreshSlider), app.query_one(ScreenView)
        stops = rate._stops()
        await pilot.click(RefreshSlider, offset=(stops[-1], 0))
        await pilot.pause(0.1)
        assert sv.refresh_s == 60.0
        await pilot.click(RefreshSlider, offset=(stops[0], 0))
        await pilot.pause(0.1)
        assert sv.refresh_s is None
    with_app(body)


def test_typing_a_name_or_a_number_jumps_to_that_entry():
    async def body(app, pilot, q, rt):
        await pilot.press("f5")
        reply(q, MAIN)
        await pilot.pause(0.3)
        sv = app.query_one(ScreenView)
        await pilot.press("l")
        assert MAIN["options"][sv.sel] == "LoRa"
        await pilot.pause(1.0)
        await pilot.press("r", "f")
        assert MAIN["options"][sv.sel] == "RFID"
        await pilot.pause(1.0)
        await pilot.press("1", "2")
        assert sv.sel == 12
        await pilot.press("home")
        assert sv.sel == 0
        await pilot.press("end")
        assert sv.sel == len(MAIN["options"]) - 1
        assert sent(rt) == ["screen view"]                                # all of that was local
    with_app(body)


def test_inside_an_app_the_arrows_become_buttons_and_the_screen_refreshes_itself():
    async def body(app, pilot, q, rt):
        app.query_one(ScreenView).refresh_s = 0.4
        await pilot.press("f5")
        reply(q, IBEACON)
        await pilot.pause(0.3)
        await pilot.press("right")
        await pilot.pause(0.1)
        assert sent(rt)[-1] == "screen view next"
        reply(q, IBEACON)
        await pilot.pause(0.3)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert sent(rt)[-1] == "screen view sel"
        reply(q, IBEACON)
        n = len(sent(rt))
        await pilot.pause(1.4)                                            # nobody touched anything
        assert len(sent(rt)) > n and sent(rt)[-1] == "screen view"        # it looked again on its own
    with_app(body)
    screenview.REQUEST_TIMEOUT_S = 15.0


def test_screen_mode_waits_for_the_link_instead_of_sending_into_the_void():
    async def body(app, pilot, q, rt):
        q.put(("snap", dict(SNAP, state="search")))
        await pilot.pause(0.3)
        await pilot.press("f5")
        await pilot.pause(0.2)
        assert sent(rt) == []
        assert "Not linked" in str(app.query_one(ScreenView).render())
        q.put(("snap", dict(SNAP)))
        await pilot.pause(0.3)
        await pilot.press("enter")                                        # nothing loaded yet: asks again
        await pilot.pause(0.1)
        assert sent(rt) == ["screen view"]
    with_app(body)


def test_end_to_end_against_the_demo_core2_walks_into_an_app_and_back_out():
    async def go():
        q = queue.SimpleQueue()
        cb = dict(on_output=lambda b: q.put(("out", b)), on_events=lambda e: q.put(("events", e)),
                  on_snapshot=lambda s: q.put(("snap", s)))
        rt = DemoRuntime(Link(LinkConfig(auto=False, target=4)), speed=25.0, walk=False, **cb)
        app = BruceApp(rt, q, demo=True)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(2.5)
            await pilot.press("f5")
            sv = app.query_one(ScreenView)
            await pilot.pause(2.0)
            assert sv.mode == "menu" and sv.options[1] == "BLE"
            await pilot.press("b")                                        # type-ahead -> BLE
            await pilot.press("enter")
            await pilot.pause(2.0)
            assert sv.title == "Bluetooth" and sv.path == ["BLE"]
            await pilot.press("down", "down", "enter")                    # iBeacon
            await pilot.pause(2.0)
            assert sv.mode == "app" and any("IBEACON" in l for l in sv.lines)
            await pilot.press("escape")
            await pilot.pause(2.0)
            assert sv.mode == "menu" and sv.path == []
    asyncio.run(go())
