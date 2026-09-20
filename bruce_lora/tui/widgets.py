"""Widgets for the Bruce LoRa remote: black background, one light-blue accent, nothing else shouting."""
from __future__ import annotations

import re
from collections import deque
from typing import Callable, Optional

from rich.console import Group
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ..protocol import PROFILES, airtime_ms, data_budget

# ---- palette ---------------------------------------------------------------------------------
ACCENT = "#7dc8ff"        # the accent: light blue
ACCENT_DIM = "#3b7ca8"
ACCENT_FAINT = "#12324a"
TEXT = "#cfe0ec"
DIM = "#6b8496"
MUTE = "#34444f"
WARN = "#ffb454"
BAD = "#ff6b6b"

BARS = "▁▂▃▄▅▆▇█"


def bar(frac: float, width: int, on: str = "▮", off: str = "▯") -> str:
    n = max(0, min(width, round(frac * width)))
    return on * n + off * (width - n)


def rssi_frac(rssi: Optional[float]) -> float:
    return 0.0 if rssi is None else max(0.0, min(1.0, (rssi + 130) / 90))     # -130 dBm .. -40 dBm


def signal_bars(rssi: Optional[float]) -> str:
    if rssi is None:
        return "▁▁▁▁"
    lit = max(0, min(4, int(rssi_frac(rssi) * 4.99)))
    return "".join(BARS[1 + 2 * i] if i < lit else BARS[0] for i in range(4))


def sparkline(values, width: int, lo: float = -130, hi: float = -30) -> str:
    vals = list(values)[-width:]
    out = []
    for v in vals:
        f = max(0.0, min(1.0, (v - lo) / (hi - lo)))
        out.append(BARS[min(7, int(f * 7.99))])
    return "".join(out).rjust(width, BARS[0]) if not vals else "".join(out).rjust(width, " ")


def sparkline2(values, width: int, lo: float = -130, hi: float = -30) -> tuple[str, str]:
    """Two stacked rows of block characters = 16 levels of vertical resolution."""
    top, bot = [], []
    for v in list(values)[-width:]:
        lvl = int(max(0.0, min(1.0, (v - lo) / (hi - lo))) * 15.99)      # 0..15
        bot.append(BARS[min(7, lvl)])
        top.append(BARS[lvl - 8] if lvl >= 8 else " ")
    return "".join(top).rjust(width), "".join(bot).rjust(width)


def fmt_bytes(n: float) -> str:
    return f"{n:.0f} B" if n < 1024 else f"{n / 1024:.1f} KB"


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def throughput_estimate(profile: int) -> float:
    """Rough sustained device->controller output rate (B/s): one small poll + one full frame per exchange."""
    p = PROFILES[profile]
    exch = airtime_ms(profile, 16) + airtime_ms(profile, p.max_frame) + 2 * 60 + 2 * 40
    return data_budget(profile, True) * 1000.0 / exch


# ---- header ----------------------------------------------------------------------------------

class HeaderBar(Static):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.snap: dict = {}
        self.demo = False

    def update_snap(self, snap: dict) -> None:
        self.snap = snap
        self.refresh()

    def render(self) -> Text:
        s = self.snap
        width = self.size.width or 80
        left = Text()
        left.append(" ▌", style=ACCENT)
        left.append("BRUCE", style=f"bold {ACCENT}")
        left.append("  LoRa remote", style=TEXT)
        if self.demo:
            left.append("  DEMO", style=f"bold {WARN}")
        state = s.get("state", "search")
        right = Text()
        if state == "linked":
            degraded = s.get("fail", 0) > 0.25
            right.append("● ", style=WARN if degraded else ACCENT)
            right.append("DEGRADED" if degraded else "LINKED", style=f"bold {WARN if degraded else ACCENT}")
            p = PROFILES[s.get("profile", 0)]
            right.append(f"   {p.name}", style=TEXT)
            right.append(f" {'⇄' if s.get('switching') else '·'} ", style=WARN if s.get("switching") else DIM)
            right.append(f"{'auto' if s.get('auto') else 'manual'}", style=DIM)
            if s.get("link_since") is not None and s.get("now") is not None:
                right.append(f"   {fmt_dur((s['now'] - s['link_since']) / 1000)}", style=DIM)
        elif state == "nomodule":
            right.append("✕ ", style=BAD)
            right.append("NO MODULE", style=f"bold {BAD}")
        else:
            right.append("◌ ", style=WARN)
            right.append("SEARCHING", style=f"bold {WARN}")
        right.append("   " + str(s.get("port", "")).replace("/dev/cu.", ""), style=DIM)
        right.append(" ")
        pad = max(1, width - left.cell_len - right.cell_len)
        out = Text()
        out.append_text(left)
        out.append(" " * pad)
        out.append_text(right)
        return out


# ---- terminal --------------------------------------------------------------------------------

_HEAD = re.compile(r"^[A-Za-z][A-Za-z0-9 /&-]*:$")
_ENTRY = re.compile(r"^(\s+)(\S.*?)(\s+[-–]\s+)(.*)$")
_KV = re.compile(r"^([A-Za-z][A-Za-z0-9 _/.-]{1,28}):\s+(.*)$")


def style_line(line: str) -> Text:
    t = Text(no_wrap=False)
    if line.startswith("# ") or line == "#":
        t.append(line, style=ACCENT_DIM)
    elif line.startswith("· "):                          # notes from this app, not from the Core2
        t.append("· ", style=ACCENT_DIM)
        t.append(line[2:], style=DIM)
    elif line.startswith("❯ "):
        t.append("❯ ", style=f"bold {ACCENT}")
        t.append(line[2:], style=f"bold {TEXT}")
    elif re.match(r"^(ERROR|Error|error|Unknown|Failed|\[lora)", line):
        t.append(line, style=BAD if not line.startswith("[lora") else WARN)
    elif _HEAD.match(line):
        t.append(line, style=f"bold {ACCENT}")
    elif (m := _ENTRY.match(line)):
        t.append(m[1])
        t.append(m[2], style=ACCENT)
        t.append(m[3], style=MUTE)
        t.append(m[4], style=TEXT)
    elif (m := _KV.match(line)):
        t.append(m[1] + ": ", style=DIM)
        t.append(m[2], style=TEXT)
    elif line.startswith(">"):
        t.append(line, style=f"bold {ACCENT}")          # the highlighted menu entry in `nav` output
    else:
        t.append(line, style=TEXT)
    return t


class TerminalView(VerticalScroll):
    """Scrollback of what the Core2 printed (plus the commands you sent), with a live prompt line."""
    can_focus = False

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.lines: deque[str] = deque(maxlen=4000)
        self.partial = ""
        self.body = Static(expand=True)
        self.line_filter: Optional[Callable[[str], bool]] = None   # True = the line was taken by someone else

    def compose(self):
        yield self.body

    def on_mount(self) -> None:
        self.border_subtitle = "output only — type in the box below"
        self._paint()

    def on_click(self) -> None:
        """This pane is output only: a click hands you the real input instead of a dead area."""
        self.scroll_end(animate=False)
        try:
            self.app.query_one("#cmd").focus()
        except Exception:
            pass

    def feed(self, data: bytes) -> None:
        text = self.partial + data.decode("utf-8", "replace").replace("\r", "")
        parts = text.split("\n")
        self.partial = parts.pop()
        for p in parts:
            if self.line_filter and self.line_filter(p):
                continue
            self.lines.append(p)
        self._paint()

    def echo(self, cmd: str) -> None:
        if self.partial:                        # the prompt "# " is on screen: finish that line with the command
            self.lines.append(self.partial + cmd)
            self.partial = ""
        else:
            self.lines.append("❯ " + cmd)
        self._paint()

    def note(self, msg: str) -> None:
        self.lines.append(f"· {msg}")
        self._paint()

    def clear(self) -> None:
        self.lines.clear()
        self.partial = ""
        self._paint()

    def _paint(self, scroll: bool = True) -> None:
        out = Text(no_wrap=False)
        for i, line in enumerate(self.lines):
            if i:
                out.append("\n")
            out.append_text(style_line(line))
        if self.lines:
            out.append("\n")
        out.append_text(style_line(self.partial) if self.partial else Text(""))
        at_bottom = self.scroll_y >= self.max_scroll_y - 2
        self.body.update(out)
        if scroll and at_bottom:
            self.scroll_end(animate=False)
            self.border_subtitle = "output only — type in the box below"
        elif scroll:
            self.border_subtitle = "↓ new output — click to jump to the end"


# ---- signal ----------------------------------------------------------------------------------

class SignalPanel(Static):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.snap: dict = {}
        self.hist_dn: deque[float] = deque(maxlen=120)
        self.hist_up: deque[float] = deque(maxlen=120)

    def update_snap(self, snap: dict) -> None:
        self.snap = snap
        if snap.get("state") == "linked":
            dn, up = snap.get("dn", (None, None)), snap.get("up", (None, None))
            if dn[0] is not None:
                self.hist_dn.append(dn[0])
            if up[0] is not None:
                self.hist_up.append(up[0])
        self.refresh()

    def render(self) -> Text:
        s = self.snap
        w = max(16, (self.size.width or 34) - 2)
        dn, up = s.get("dn", (None, None)), s.get("up", (None, None))
        t = Text()
        linked = s.get("state") == "linked" and dn[0] is not None
        hot = lambda r: r is not None and r > -35

        def row(label: str, rssi, snr) -> None:
            t.append(f" {label:<12}", style=DIM)
            if rssi is None or not linked:
                t.append("no module" if s.get("state") == "nomodule" else "no signal", style=MUTE)
            else:
                style = WARN if hot(rssi) else ACCENT
                t.append(signal_bars(rssi) + " ", style=style)
                t.append(f"{rssi:>4} dBm", style=f"bold {TEXT}")
                t.append(f"  SNR {snr:+d}", style=DIM)
            t.append("\n")

        row("Core2 → you", dn[0], dn[1])
        row("you → Core2", up[0], up[1])
        m = s.get("margin")
        t.append(" Margin      ", style=DIM)
        if m is None or not linked:
            t.append("—", style=MUTE)
        else:
            col = BAD if m < 6 else WARN if m < 12 else ACCENT
            t.append(bar(min(1.0, m / 40), 10) + " ", style=col)
            t.append(f"{m:.0f} dB", style=f"bold {TEXT}")
        t.append("\n")
        rtt = s.get("rtt_ms") or 0
        t.append(" Round trip  ", style=DIM)
        t.append(f"{rtt / 1000:.1f} s" if rtt else "—", style=TEXT)
        t.append("    Retries  ", style=DIM)
        t.append(f"{s.get('retries', 0)}", style=WARN if s.get("fail", 0) > 0.25 else TEXT)
        t.append("\n")
        top, bot = sparkline2(self.hist_dn, w - 2)
        t.append(" " + top + "\n", style=ACCENT_DIM)
        t.append(" " + bot + "\n", style=ACCENT_DIM)
        t.append(" " + "◂ downlink history".ljust(w - 6), style=MUTE)
        t.append("now", style=MUTE)
        if linked and (hot(dn[0]) or hot(up[0])):
            at_min = s.get("dev_power", 22) <= 2 and s.get("ctl_power", 22) <= 2
            t.append("\n ⚠ radios very close — lowest power" if at_min else "\n ⚠ signal very hot — lowering power",
                     style=WARN)
        return t


# ---- range / speed slider --------------------------------------------------------------------

class RangeSlider(Static):
    """←/→ pick a profile (turns auto off), `a` toggles auto. Click a stop to jump to it."""
    can_focus = True
    BINDINGS = [
        Binding("left", "move(-1)", "slower / longer range", show=False),
        Binding("right", "move(1)", "faster", show=False),
        Binding("a", "toggle_auto", "auto", show=False),
        Binding("enter", "apply", "apply", show=False),
    ]

    class Chosen(Message):
        def __init__(self, profile: Optional[int]) -> None:
            super().__init__()
            self.profile = profile          # None = auto

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.snap: dict = {}
        self.sel: Optional[int] = None      # profile the user is pointing at (before applying)
        self._timer = None

    def update_snap(self, snap: dict) -> None:
        self.snap = snap
        if self._timer is None:
            self.sel = None
        self.refresh()

    def _shown(self) -> int:
        return self.sel if self.sel is not None else self.snap.get("profile", 0)

    def action_move(self, d: int) -> None:
        cur = self._shown()
        self.sel = max(0, min(len(PROFILES) - 1, cur + d))
        if self._timer:
            self._timer.stop()
        self._timer = self.set_timer(0.45, self.action_apply)      # settle, then apply
        self.refresh()

    def action_apply(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None
        if self.sel is not None:
            self.post_message(self.Chosen(self.sel))

    def action_toggle_auto(self) -> None:
        self.post_message(self.Chosen(None if not self.snap.get("auto") else self.snap.get("profile", 0)))

    def _track(self, width: int) -> tuple[int, int]:
        x0 = 3
        return x0, max(x0 + 10, width - 4)

    def on_click(self, event: events.Click) -> None:
        if event.y != 3:                       # the track is the 4th line of the render below
            if event.y == 0 and event.x >= self.size.width - 12:
                self.action_toggle_auto()
            return
        x0, x1 = self._track(self.size.width)
        frac = (event.x - x0) / max(1, x1 - x0)
        self.sel = max(0, min(len(PROFILES) - 1, round(frac * (len(PROFILES) - 1))))
        self.focus()
        self.action_apply()

    def render(self) -> Text:
        s = self.snap
        w = self.size.width or 34
        idx = self._shown()
        p = PROFILES[idx]
        active = s.get("profile", 0)
        auto = bool(s.get("auto"))
        pending = self.sel is not None and self.sel != active
        t = Text()
        # line 1: name and auto pill
        t.append(f" {p.name:<9}", style=f"bold {ACCENT}")
        if pending or s.get("switching"):
            t.append("switching…" if s.get("switching") else "release to apply", style=WARN)
        pill = " AUTO " if auto else " MANUAL "
        pad = max(1, w - t.cell_len - len(pill) - 2)
        t.append(" " * pad)
        t.append(pill, style=f"bold black on {ACCENT}" if auto else f"{DIM} on #10181f")
        t.append("\n\n")
        # line 3: legend
        t.append(" range".ljust(w - 8), style=MUTE)
        t.append("speed\n", style=MUTE)
        # line 4: track  (row index 4 counting from 0 with the blank line above: keep in sync with on_click)
        x0, x1 = self._track(w)
        span = x1 - x0
        line = [" "] * w
        stops = [x0 + round(span * i / (len(PROFILES) - 1)) for i in range(len(PROFILES))]
        for x in range(x0, x1 + 1):
            line[x] = "━"
        for i, x in enumerate(stops):
            line[x] = "┿" if i != idx else "●"
        # mark where the link really is when the user is pointing elsewhere
        if pending:
            line[stops[active]] = "◉"
        row = Text("".join(line))
        row.stylize(ACCENT_DIM, x0, x1 + 1)
        row.stylize(f"bold {ACCENT}", stops[idx], stops[idx] + 1)
        if pending:
            row.stylize(WARN, stops[active], stops[active] + 1)
        t.append_text(row)
        t.append("\n")
        names = Text(" " * w)
        short = ("Range", "Long", "Std", "Fast", "Quick", "Turbo")
        for i, x in enumerate(stops):
            label = short[i]
            pos = max(0, min(w - len(label), x - len(label) // 2))
            names.plain = names.plain[:pos] + label + names.plain[pos + len(label):]
        names.stylize(MUTE)
        cur = short[idx]
        cpos = max(0, min(w - len(cur), stops[idx] - len(cur) // 2))
        names.stylize(f"bold {ACCENT}", cpos, cpos + len(cur))
        t.append_text(names)
        t.append("\n")
        # details
        t.append(f" SF{p.sf}  {p.bw_hz // 1000} kHz  CR 4/{p.cr + 4}   ", style=TEXT)
        t.append(f"{p.max_frame} B frames\n", style=DIM)
        t.append(f" {airtime_ms(idx, p.max_frame) / 1000:.2f} s per frame   ", style=TEXT)
        t.append(f"≈{throughput_estimate(idx):.0f} B/s\n", style=f"bold {TEXT}")
        t.append(f" sensitivity {p.sensitivity_dbm:.0f} dBm", style=DIM)
        return t


# ---- power -----------------------------------------------------------------------------------

class PowerPanel(Static):
    can_focus = True
    BINDINGS = [
        Binding("up", "row(-1)", show=False), Binding("down", "row(1)", show=False),
        Binding("left", "adj(-2)", show=False), Binding("right", "adj(2)", show=False),
        Binding("a", "toggle_auto", show=False),
    ]

    class Set(Message):
        def __init__(self, who: str, dbm: int) -> None:
            super().__init__()
            self.who, self.dbm = who, dbm

    class Auto(Message):
        def __init__(self, on: bool) -> None:
            super().__init__()
            self.on = on

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.snap: dict = {}
        self.row = 0

    def update_snap(self, snap: dict) -> None:
        self.snap = snap
        self.refresh()

    def action_row(self, d: int) -> None:
        self.row = (self.row + d) % 2
        self.refresh()

    def action_adj(self, d: int) -> None:
        who = "dev" if self.row == 0 else "ctl"
        cur = self.snap.get("dev_power" if who == "dev" else "ctl_power", 22)
        self.post_message(self.Set(who, max(0, min(22, cur + d))))

    def action_toggle_auto(self) -> None:
        self.post_message(self.Auto(not self.snap.get("auto_power", True)))

    def on_click(self, event: events.Click) -> None:
        if event.y in (0, 1):
            self.row = event.y
            self.focus()
            self.refresh()

    def render(self) -> Text:
        s = self.snap
        t = Text()
        auto = s.get("auto_power", True)
        for i, (label, key) in enumerate((("Core2", "dev_power"), ("Here", "ctl_power"))):
            dbm = s.get(key, 22)
            sel = self.has_focus and self.row == i
            t.append(" ▸ " if sel else "   ", style=ACCENT)
            t.append(f"{label:<6}", style=f"bold {TEXT}" if sel else TEXT)
            t.append(bar(dbm / 22, 11), style=ACCENT if dbm > 3 else ACCENT_DIM)
            t.append(f" {dbm:>2} dBm\n", style=TEXT)
        t.append("   ", style=DIM)
        t.append("auto ", style=DIM)
        t.append("on" if auto else "off", style=f"bold {ACCENT}" if auto else MUTE)
        t.append("  ·  protects close-range links", style=MUTE)
        return t


# ---- session ---------------------------------------------------------------------------------

class SessionPanel(Static):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.snap: dict = {}
        self._last: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.rate_in = self.rate_out = 0.0

    def update_snap(self, snap: dict) -> None:
        import time
        now = time.monotonic()
        bi, bo = snap.get("bytes_in", 0), snap.get("bytes_out", 0)
        t0, i0, o0 = self._last
        if now - t0 >= 2.0:
            if t0:
                self.rate_in, self.rate_out = (bi - i0) / (now - t0), (bo - o0) / (now - t0)
            self._last = (now, bi, bo)
        self.snap = snap
        self.refresh()

    def render(self) -> Text:
        s = self.snap
        t = Text()

        def kv(k: str, v: str, style: str = TEXT) -> None:
            t.append(f" {k:<10}", style=DIM)
            t.append(v, style=style)

        kv("received", f"{fmt_bytes(s.get('bytes_in', 0))}")
        t.append(f"   {self.rate_in:.0f} B/s\n", style=MUTE)
        kv("sent", f"{fmt_bytes(s.get('bytes_out', 0))}")
        t.append(f"   {self.rate_out:.0f} B/s\n", style=MUTE)
        kv("frames", f"{s.get('tx', 0)} out  {s.get('rx', 0)} in\n")
        bad, to = s.get("bad", 0), s.get("timeouts", 0)
        kv("lost", f"{to} timeouts  {bad} corrupt", WARN if (to or bad) else TEXT)
        if s.get("queued"):
            t.append(f"\n queued {s['queued']} B", style=DIM)
        return t


# ---- nav pad ---------------------------------------------------------------------------------

class NavPad(Static):
    """Buttons for the Core2's menu (its screen is out of sight): they send `nav prev|next|sel|esc`."""

    class Pressed(Message):
        def __init__(self, cmd: str) -> None:
            super().__init__()
            self.cmd = cmd

    KEYS = [("◀ prev", "prev"), ("● select", "sel"), ("next ▶", "next"), ("✕ back", "esc")]

    def on_click(self, event: events.Click) -> None:
        if event.y != 0:
            return
        w = self.size.width or 34
        cell = w // len(self.KEYS)
        idx = min(len(self.KEYS) - 1, event.x // max(1, cell))
        self.post_message(self.Pressed(self.KEYS[idx][1]))

    def render(self) -> Text:
        w = self.size.width or 34
        cell = max(8, w // len(self.KEYS))
        t = Text()
        for label, _ in self.KEYS:
            t.append(label.center(cell - 1), style=f"{ACCENT} on #0c1a26")
            t.append(" ")
        return t
