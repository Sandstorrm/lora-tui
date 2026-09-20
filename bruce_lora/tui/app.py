"""The full-screen remote: a terminal for the Core2's CLI, link and power panels, a range/speed slider."""
from __future__ import annotations

import queue
import time
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, Input, RichLog, Static

from ..engine import Event
from ..protocol import PROFILES
from .screenview import RefreshSlider, ScreenView, parse_screen
from .widgets import (ACCENT, ACCENT_DIM, BAD, DIM, MUTE, TEXT, WARN, HeaderBar, NavPad, PowerPanel, RangeSlider,
                      SessionPanel, SignalPanel, TerminalView)

BRUCE_THEME = Theme(
    name="bruce", primary=ACCENT, secondary=ACCENT_DIM, accent=ACCENT, foreground=TEXT, background="#000000",
    surface="#04070a", panel="#07101a", warning=WARN, error=BAD, success=ACCENT, dark=True,
)

COMMANDS = ["help", "uptime", "free", "info", "nav", "options", "wifi", "arp", "listen", "sniffer", "webui", "ir",
            "subghz", "rf", "music_player", "tone", "say", "led", "clock", "power", "gpio", "i2c", "storage", "ls",
            "settings", "display", "optionsJSON", "reboot", "factory_reset"]

HELP_TEXT = f"""[bold {ACCENT}]Typing[/]        commands go straight to the Core2's CLI (try [{ACCENT}]help[/], [{ACCENT}]uptime[/], [{ACCENT}]nav next[/])
[bold {ACCENT}]↑ ↓[/]           command history            [bold {ACCENT}]Tab[/]  complete a command name
[bold {ACCENT}]Ctrl+C[/]        drop the Core2's pending output (when a long reply is in flight)
[bold {ACCENT}]Ctrl+L[/]        clear the terminal         [bold {ACCENT}]Ctrl+R[/]  reconnect
[bold {ACCENT}]F2[/]            range / speed slider: ← → change, A auto/manual, click a stop
[bold {ACCENT}]F3[/]            power: ↑ ↓ pick a side, ← → change, A auto
[bold {ACCENT}]F4[/]            packet log                 [bold {ACCENT}]F1 / Esc[/]  back to the command line
[bold {ACCENT}]F5[/]            [bold]screen mode[/]: the Core2's menus and apps as a keyboard view in place of the terminal
                ↑↓ move · Enter open · Esc back · type a name or number to jump · inside an app ←/→ Enter Esc
                [ ] or Tab: the auto-refresh slider that replaces the command line in screen mode
[bold {ACCENT}]F6 F7 F8 F9[/]   Core2 menu: prev · select · next · back  (or click the pad)
[bold {ACCENT}]Ctrl+T[/]        auto range/speed on/off    [bold {ACCENT}]Ctrl+P[/]  auto power on/off
[bold {ACCENT}]Ctrl+Q[/]        quit

[bold {ACCENT}]:profile[/] n|name|auto   [bold {ACCENT}]:power[/] dev|ctl dBm|auto   [bold {ACCENT}]:flush[/]  [bold {ACCENT}]:resync[/]  [bold {ACCENT}]:clear[/]  [bold {ACCENT}]:quit[/]

[{DIM}]Range = slowest, longest reach (the RYLR998's best sensitivity). Turbo = fastest, shortest.
In auto the controller picks the fastest profile the link can carry with margin to spare, and
lowers TX power when the radios are so close that the receivers overload.[/]"""


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", show=False), Binding("f10", "dismiss", show=False),
                Binding("q", "dismiss", show=False)]

    def compose(self) -> ComposeResult:
        box = Static(HELP_TEXT, id="help-box", markup=True)
        box.border_title = "Keys"
        yield box

    def on_click(self) -> None:
        self.dismiss()


class CommandInput(Input):
    BINDINGS = [Binding("up", "history(-1)", show=False), Binding("down", "history(1)", show=False),
                Binding("tab", "complete", show=False)]

    def __init__(self, **kw) -> None:
        super().__init__(placeholder="type a Bruce command — Enter to send    (↑↓ history · Tab complete · F10 keys)",
                         **kw)
        self.history: list[str] = []
        self.pos = 0

    def remember(self, text: str) -> None:
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.pos = len(self.history)

    def action_history(self, d: int) -> None:
        if not self.history:
            return
        self.pos = max(0, min(len(self.history), self.pos + d))
        self.value = self.history[self.pos] if self.pos < len(self.history) else ""
        self.cursor_position = len(self.value)

    def action_complete(self) -> None:
        v = self.value
        if " " in v or not v:
            return
        hits = [c for c in COMMANDS if c.startswith(v)]
        if hits:
            common = hits[0]
            for h in hits[1:]:
                while not h.startswith(common):
                    common = common[:-1]
            self.value = common + (" " if len(hits) == 1 else "")
            self.cursor_position = len(self.value)


def describe(frame) -> str:
    k = frame.kind
    if k == "D":
        bits = "".join(c for c, f in (("M", 1), ("S", 2), ("F", 4)) if frame.flags & f)
        n = len(frame.payload)
        return f"DATA  seq {frame.seq:X} ack {frame.ack:X}  {n:>3} B" + (f"  [{bits}]" if bits else "")
    return {"H": lambda: f"HELLO  sid {frame.sid:X}{' fresh' if frame.fresh else ' resume'}",
            "W": lambda: f"WELCOME  {PROFILES[frame.cur].name} pwr {frame.power}{' resumed' if frame.res else ''}",
            "P": lambda: f"SET PROFILE → {PROFILES[frame.target].name}",
            "Q": lambda: f"PROFILE OK  {PROFILES[frame.target].name}",
            "T": lambda: f"SET POWER → {frame.power} dBm",
            "U": lambda: f"POWER OK  {frame.power} dBm",
            "N": lambda: "NO SESSION"}[k]()


class BruceApp(App):
    CSS_PATH = str(Path(__file__).with_name("theme.tcss"))
    TITLE = "Bruce LoRa remote"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("f1", "focus_cmd", "command", show=False),
        Binding("escape", "focus_cmd", "command", show=False),
        Binding("f2", "focus_slider", "speed"),
        Binding("f3", "focus_power", "power"),
        Binding("f4", "toggle_packets", "packets"),
        Binding("f5", "toggle_screen", "screen"),
        Binding("ctrl+c", "flush", "drop output"),
        Binding("ctrl+l", "clear", "clear"),
        Binding("ctrl+r", "resync", "reconnect"),
        Binding("ctrl+t", "toggle_auto", "auto speed"),
        Binding("ctrl+p", "toggle_auto_power", "auto power"),
        Binding("f6", "nav('prev')", "", show=False), Binding("f7", "nav('sel')", "", show=False),
        Binding("f8", "nav('next')", "", show=False), Binding("f9", "nav('esc')", "", show=False),
        Binding("f10", "help", "keys"),
    ]

    def __init__(self, runtime, q: "queue.SimpleQueue", demo: bool = False, log_path=None) -> None:
        super().__init__()
        self.rt, self.q, self.demo, self.log_path = runtime, q, demo, log_path
        self.snap: dict = {}
        self._t0 = time.monotonic()
        self._was_linked = False
        self._awaiting: Optional[float] = None     # when we last sent something and are still waiting for output
        self.busy_hint_s = 10.0

    # ---- layout ----------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield HeaderBar(id="hdr")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static(id="nomod")
                yield TerminalView(id="term")
                yield ScreenView(id="screen")
                yield CommandInput(id="cmd")
                yield RefreshSlider(id="rate")
                yield RichLog(id="packets", max_lines=400, wrap=False, markup=False, highlight=False)
            with Vertical(id="side"):
                yield SignalPanel(id="signal", classes="panel")
                yield RangeSlider(id="slider", classes="panel")
                yield PowerPanel(id="power", classes="panel")
                yield NavPad(id="nav", classes="panel")
                yield SessionPanel(id="session", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(BRUCE_THEME)
        self.theme = "bruce"
        self.query_one("#term").border_title = "Core2 terminal"
        self.query_one("#screen").border_title = "Core2 screen"
        self.query_one("#packets").border_title = "Packets"
        self.query_one("#packets").display = False
        self.query_one("#screen").display = False
        self.query_one("#rate").display = False
        self.query_one("#nomod").display = False
        self.query_one("#term", TerminalView).line_filter = self._take_screen_line
        for id_, title in (("signal", "Signal"), ("slider", "Range ◂▸ Speed"), ("power", "TX power"),
                           ("nav", "Core2 menu"), ("session", "Session")):
            self.query_one(f"#{id_}").border_title = title
        self.query_one("#hdr", HeaderBar).demo = self.demo
        self.query_one("#term", TerminalView).note(
            "demo: a simulated Core2 — the virtual radio walks near, far and back" if self.demo else "connecting…")
        if self.log_path:
            self.query_one("#term", TerminalView).note(f"logging to {str(self.log_path).replace(str(Path.home()), '~')}")
        self.rt.start()
        self.set_interval(1 / 30, self._drain)
        self.query_one(CommandInput).focus()

    def on_unmount(self) -> None:
        self.rt.stop()

    # ---- runtime -> UI ---------------------------------------------------------------------
    def _drain(self) -> None:
        try:
            term = self.query_one("#term", TerminalView)
        except NoMatches:                       # the timer can fire once more while the app is shutting down
            return
        try:
            for _ in range(300):
                kind, payload = self.q.get_nowait()
                if kind == "out":
                    self._awaiting = None
                    term.feed(payload)
                elif kind == "events":
                    for ev in payload:
                        self._on_event(ev)
                elif kind == "snap":
                    self._on_snap(payload)
        except queue.Empty:
            pass
        if (self._awaiting is not None and time.monotonic() - self._awaiting > self.busy_hint_s
                and self.snap.get("state") == "linked"):
            self._awaiting = None
            term.note("no reply yet, but the link is fine — the Core2 may be busy (for example sitting in a menu "
                      "it opened itself). Tap its screen, or use nav / options.")
        if getattr(self.rt, "error", None):
            self.notify(str(self.rt.error), severity="error", timeout=10)
            self.rt.error = None

    def _take_screen_line(self, line: str) -> bool:
        """`screen view` answers are for the screen view, not for the terminal scrollback."""
        d = parse_screen(line)
        if d is None:
            return False
        self.query_one("#screen", ScreenView).apply(d)
        return True

    def _set_module(self, ok: bool, why: str = "") -> None:
        banner = self.query_one("#nomod", Static)
        banner.display = not ok
        self.query_one("#side").set_class(not ok, "offline")
        if not ok:
            banner.update(Text.assemble(("✕ Radio module not connected", f"bold {BAD}"),
                                        (f"  {why}\n" if why else "\n", DIM),
                                        ("Plug the RYLR998 USB adapter in — it is picked up automatically.", TEXT)))

    def _on_snap(self, snap: dict) -> None:
        self.snap = snap
        ok = snap.get("module", True)
        if ok != getattr(self, "_module_ok", True) or (not ok and not self.query_one("#nomod").display):
            self._module_ok = ok
            self._set_module(ok, snap.get("module_error") or "")
        self.query_one("#screen", ScreenView).set_linked(snap.get("state") == "linked")
        self.query_one("#hdr", HeaderBar).update_snap(snap)
        for id_ in ("signal", "slider", "power", "session"):
            self.query_one(f"#{id_}").update_snap(snap)

    def _on_event(self, ev: Event) -> None:
        log = self.query_one("#packets", RichLog)
        term = self.query_one("#term", TerminalView)
        i, ts = ev.info, f"{(ev.t / 1000) % 3600:7.2f}"
        k = ev.kind
        if k in ("tx", "rx"):
            f = i.get("frame")
            line = Text()
            line.append(ts + "  ", style=MUTE)
            if k == "tx":
                line.append("▶ ", style=ACCENT)
                line.append(describe(f), style=TEXT)
                if i.get("retry"):
                    line.append(f"  retry {i['retry']}", style=WARN)
                line.append(f"   {PROFILES[i['profile']].name}", style=MUTE)
            elif f is None:
                line.append("✕ ", style=BAD)
                line.append(f"corrupt frame ({len(i['raw'])} B)  {i['rssi']} dBm", style=BAD)
            else:
                line.append("◀ ", style=DIM)
                line.append(describe(f), style=TEXT)
                line.append(f"   {i['rssi']} dBm  SNR {i['snr']:+d}", style=MUTE)
            log.write(line)
        elif k == "timeout":
            log.write(Text(f"{ts}  ⏱ no reply ({i['what']}, try {i['tries']}, {PROFILES[i['profile']].name})", style=WARN))
        elif k == "state":
            if i["state"] == "linked":
                term.note(f"linked · {PROFILES[i['profile']].name}" + (" (session resumed)" if i.get("resumed") else ""))
                self.notify(f"Linked to the Core2 on {PROFILES[i['profile']].name}", timeout=3)
                self._was_linked = True
            elif self._was_linked:
                term.note("lost the Core2 — searching…")
                self.notify("Lost the Core2 — searching on every profile", severity="warning", timeout=4)
        elif k == "module":
            if i["ok"]:
                term.note("radio module connected — searching for the Core2…")
                self.notify("Radio module connected", timeout=4)
            else:
                term.note(f"radio module lost: {i['why']}")
                self.notify("Radio module disconnected — plug it back in", severity="error", timeout=8)
        elif k == "profile":
            self.notify(f"Range/speed → {PROFILES[i['to']].name}", timeout=3)
        elif k == "switch_failed":
            self.notify(f"{PROFILES[i['to']].name} isn't reachable from here — staying on {PROFILES[i['frm']].name}",
                        severity="warning", timeout=5)
        elif k == "power":
            who = "Core2" if i["who"] == "dev" else "Your radio"
            self.notify(f"{who} TX power → {i['dbm']} dBm", timeout=3)
        elif k == "notice":
            self.notify(i["text"], severity="warning", timeout=4)

    # ---- UI -> runtime ---------------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        inp = self.query_one(CommandInput)
        text = event.value
        inp.value = ""
        if text.startswith(":"):
            self._local(text[1:].strip())
            return
        if not self.snap.get("module", True):
            self.query_one("#term", TerminalView).note("no radio module — command not sent")
            return
        inp.remember(text.strip())
        self.query_one("#term", TerminalView).echo(text)
        self._awaiting = time.monotonic()
        self.rt.send(text + "\n")

    def _local(self, cmd: str) -> None:
        parts = cmd.split()
        term = self.query_one("#term", TerminalView)
        if not parts:
            return
        op, args = parts[0], parts[1:]
        if op in ("quit", "q", "exit"):
            self.exit()
        elif op == "clear":
            term.clear()
        elif op == "flush":
            self.action_flush()
        elif op == "resync":
            self.action_resync()
        elif op == "packets":
            self.action_toggle_packets()
        elif op == "help":
            self.action_help()
        elif op == "profile" and args:
            a = args[0].lower()
            if a == "auto":
                self.rt.set_auto(True)
            else:
                idx = int(a) if a.isdigit() else next((p.idx for p in PROFILES if p.name.lower() == a), -1)
                if 0 <= idx < len(PROFILES):
                    self.rt.set_profile(idx)
                else:
                    term.note(f"unknown profile {a!r}")
        elif op == "power" and args:
            if args[0] == "auto":
                self.rt.set_auto_power(True)
            elif len(args) > 1 and args[0] in ("dev", "ctl") and args[1].isdigit():
                self.rt.set_power(args[0], int(args[1]))
            else:
                term.note("usage: :power dev|ctl <0-22>   or   :power auto")
        else:
            term.note(f"unknown local command :{op}")

    def on_refresh_slider_changed(self, msg: RefreshSlider.Changed) -> None:
        sv = self.query_one("#screen", ScreenView)
        sv.refresh_s = msg.seconds
        sv.refresh()
        self.notify("Screen auto-refresh " + ("off" if msg.seconds is None else f"every {msg.seconds:g} s"), timeout=2)

    def on_screen_view_request(self, msg: ScreenView.Request) -> None:
        self.rt.send(msg.cmd + "\n")

    def on_range_slider_chosen(self, msg: RangeSlider.Chosen) -> None:
        if msg.profile is None:
            self.rt.set_auto(True)
        else:
            self.rt.set_profile(msg.profile)

    def on_power_panel_set(self, msg: PowerPanel.Set) -> None:
        self.rt.set_power(msg.who, msg.dbm)

    def on_power_panel_auto(self, msg: PowerPanel.Auto) -> None:
        self.rt.set_auto_power(msg.on)

    def on_nav_pad_pressed(self, msg: NavPad.Pressed) -> None:
        self.action_nav(msg.cmd)

    # ---- actions ---------------------------------------------------------------------------
    def action_focus_cmd(self) -> None:
        sv = self.query_one("#screen", ScreenView)
        if sv.display:
            if self.focused is not sv:
                sv.focus()                 # in screen mode Esc/F1 come back to the screen, F5 leaves it
            return
        self.query_one(CommandInput).focus()

    def action_focus_slider(self) -> None:
        self.query_one(RangeSlider).focus()

    def action_focus_power(self) -> None:
        self.query_one(PowerPanel).focus()

    def action_toggle_screen(self) -> None:
        """Swap the terminal for the keyboard-driven screen view (and back)."""
        sv, term = self.query_one("#screen", ScreenView), self.query_one("#term", TerminalView)
        cmd, rate = self.query_one(CommandInput), self.query_one("#rate", RefreshSlider)
        if sv.display:
            sv.display, term.display = False, True
            rate.display, cmd.display = False, True
            cmd.focus()
        else:
            term.display, sv.display = False, True
            cmd.display, rate.display = False, True               # the command line means nothing here
            sv.set_linked(self.snap.get("state") == "linked")
            sv.focus()
            sv.refresh_view()

    def action_toggle_packets(self) -> None:
        p = self.query_one("#packets")
        p.display = not p.display

    def action_flush(self) -> None:
        self.rt.flush()
        self.notify("Asked the Core2 to drop its pending output", timeout=2)

    def action_clear(self) -> None:
        self.query_one("#term", TerminalView).clear()

    def action_resync(self) -> None:
        self.rt.resync()

    def action_toggle_auto(self) -> None:
        self.rt.set_auto(not self.snap.get("auto", True))

    def action_toggle_auto_power(self) -> None:
        self.rt.set_auto_power(not self.snap.get("auto_power", True))

    def action_nav(self, which: str) -> None:
        self._awaiting = time.monotonic()
        self.query_one("#term", TerminalView).echo(f"nav {which}")
        self.rt.send(f"nav {which}\n")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


def run_tui(args) -> int:
    from ..engine import Link, LinkConfig
    from ..__main__ import make_link, open_radio, try_open_radio
    from ..runtime import Runtime, now_ms
    q: "queue.SimpleQueue" = queue.SimpleQueue()
    cb = dict(on_output=lambda b: q.put(("out", b)), on_events=lambda e: q.put(("events", e)),
              on_snapshot=lambda s: q.put(("snap", s)))
    if args.demo:
        from .demo import DemoRuntime
        rt = DemoRuntime(make_link(args), speed=3.0, **cb)
    else:
        radio, why = try_open_radio(args)          # no module is not fatal: the UI shows it and keeps looking
        rt = Runtime(make_link(args, radio), radio, connect=lambda: open_radio(args), missing=why or "", **cb)
    BruceApp(rt, q, demo=args.demo, log_path=getattr(args, "log_path", None)).run()
    return 0
