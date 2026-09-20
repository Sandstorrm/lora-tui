"""Screen mode: the Core2's menus and apps as a keyboard-driven view, in place of the terminal.

Under the hood it is the same `screen view` command the terminal can run -- the Core2 reports either the menu that
is showing (title, options, cursor) or, inside an app, the text on its display. Arrow keys move a *local* cursor
(no radio traffic); Enter sends one `screen view open N`, Esc one `screen view back`.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget

from .widgets import ACCENT, ACCENT_DIM, BAD, DIM, MUTE, TEXT, WARN

REQUEST_TIMEOUT_S = 15.0
DEFAULT_REFRESH_S = 5.0
TYPEAHEAD_RESET_S = 0.9
REFRESH_STOPS = [("off", None), ("1 s", 1.0), ("2 s", 2.0), ("5 s", 5.0), ("10 s", 10.0), ("30 s", 30.0),
                 ("60 s", 60.0)]


def parse_screen(line: str) -> Optional[dict]:
    """The Core2's one-line JSON report, or None if `line` is anything else."""
    line = line.strip()
    if "{" in line and line[:line.index("{")].strip("# ") == "":   # the previous prompt is still in front of it: "# {..}"
        line = line[line.index("{"):]
    if not (line.startswith("{") and '"mode"' in line and line.endswith("}")):
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if d.get("mode") in ("menu", "app") else None


class ScreenView(Widget, can_focus=True):
    BINDINGS = [
        Binding("up", "move(-1)", show=False), Binding("down", "move(1)", show=False),
        Binding("pageup", "move(-8)", show=False), Binding("pagedown", "move(8)", show=False),
        Binding("home", "jump(0)", show=False), Binding("end", "jump(-1)", show=False),
        Binding("enter", "activate", show=False), Binding("right", "right", show=False),
        Binding("left", "back", show=False), Binding("escape", "back", show=False),
        Binding("backspace", "back", show=False),
        Binding("tab", "focus_rate", show=False),
        Binding("left_square_bracket", "rate(-1)", show=False), Binding("right_square_bracket", "rate(1)", show=False),
    ]

    class Request(Message):
        def __init__(self, cmd: str) -> None:
            super().__init__()
            self.cmd = cmd                      # e.g. "screen view open 2"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.mode = "loading"                   # loading | menu | app
        self.title = ""
        self.options: list[str] = []
        self.lines: list[str] = []
        self.sel = 0
        self.top = 0                            # first visible row of a long list
        self.path: list[str] = []               # what we opened to get here (the breadcrumb)
        self._pending: Optional[tuple[str, Optional[str]]] = None   # (kind, label) awaiting the reply
        self._busy_since: Optional[float] = None
        self._updated: Optional[float] = None
        self._typed = ""
        self._typed_at = 0.0
        self.linked = True
        self.error = ""
        self.refresh_s: Optional[float] = DEFAULT_REFRESH_S     # how often to look again by itself (None = never)

    def next_in(self) -> Optional[float]:
        """Seconds until the next automatic look, or None if refreshing is off / nothing is loaded."""
        if not self.refresh_s or self._updated is None:
            return None
        return max(0.0, self.refresh_s - (time.monotonic() - self._updated))

    # ---- lifecycle -------------------------------------------------------------------------
    def on_mount(self) -> None:
        self.border_title = "Core2 screen"
        self.set_interval(0.5, self._tick)

    def _tick(self) -> None:
        now = time.monotonic()
        if self._busy_since is not None and now - self._busy_since > REQUEST_TIMEOUT_S:
            self._busy_since = None
            self._pending = None
            self.error = "no reply from the Core2"
            self.refresh()
        elif (self.display and self.mode in ("app", "menu") and self._busy_since is None and self.linked
              and self._updated and self.refresh_s and now - self._updated > self.refresh_s):
            self.request("")                       # the screen can change on its own (an app, or a tap on the Core2)
        if self._typed and now - self._typed_at > TYPEAHEAD_RESET_S:
            self._typed = ""
            self.refresh()

    # ---- talking to the Core2 --------------------------------------------------------------
    def request(self, suffix: str, pending: Optional[tuple[str, Optional[str]]] = None) -> None:
        if not self.linked or self._busy_since is not None:
            return
        self._busy_since = time.monotonic()
        self._pending = pending
        self.error = ""
        self.post_message(self.Request(("screen view " + suffix).strip()))
        self.refresh()

    def refresh_view(self) -> None:
        self.request("")

    def apply(self, d: dict) -> None:
        """A `screen view` report arrived."""
        self._busy_since = None
        self._updated = time.monotonic()
        self.error = ""
        prev = (self.mode, self.title, tuple(self.options))
        if self._pending:
            kind, label = self._pending
            self._pending = None
            if kind == "open" and label and (d["mode"] == "app" or d.get("title") != self.title
                                             or d.get("options") != self.options):
                self.path.append(label)
            elif kind == "back":
                if self.path:
                    self.path.pop()
                if d["mode"] == "menu" and d.get("type") == "main":
                    self.path.clear()
        self.mode = d["mode"]
        self.title = d.get("title", "")
        if d["mode"] == "menu":
            self.options = list(d.get("options", []))
            if (self.mode, self.title, tuple(self.options)) != prev:
                self.sel = max(0, min(len(self.options) - 1, int(d.get("sel", 0))))
                self.top = 0
            self.sel = min(self.sel, max(0, len(self.options) - 1))
            self.lines = []
            if d.get("type") == "main":
                self.path = []
        else:
            self.lines = list(d.get("lines", []))
            self.options = []
        self.border_title = "Core2 screen — " + (" › ".join(["Main"] + self.path) if self.path else "Main")
        self.refresh()

    def set_linked(self, linked: bool) -> None:
        if linked != self.linked:
            self.linked = linked
            self.refresh()

    # ---- keys ------------------------------------------------------------------------------
    def action_focus_rate(self) -> None:
        try:
            self.app.query_one(RefreshSlider).focus()
        except Exception:
            pass

    def action_rate(self, d: int) -> None:
        try:
            self.app.query_one(RefreshSlider).action_move(d)
        except Exception:
            pass

    def action_move(self, d: int) -> None:
        if self.mode == "menu" and self.options:
            self.sel = max(0, min(len(self.options) - 1, self.sel + d))
            self.refresh()
        elif self.mode == "app":
            self.request("prev" if d < 0 else "next")

    def action_jump(self, i: int) -> None:
        if self.mode == "menu" and self.options:
            self.sel = 0 if i == 0 else len(self.options) - 1
            self.refresh()

    def action_activate(self) -> None:
        if self.mode == "menu" and self.options:
            self.request(f"open {self.sel}", ("open", self.options[self.sel]))
        elif self.mode == "app":
            self.request("sel")
        else:
            self.request("")

    def action_right(self) -> None:
        if self.mode == "menu":
            self.action_activate()
        elif self.mode == "app":
            self.request("next")

    def action_back(self) -> None:
        self.request("back", ("back", None))

    def on_key(self, event: events.Key) -> None:
        ch = event.character
        if not ch or not ch.isprintable() or ch in "[]":
            return
        if self.mode == "app":
            if ch in (" ", "r"):
                self.request("")
                event.stop()
            return
        if self.mode != "menu" or not self.options or ch == " ":
            return
        now = time.monotonic()
        if now - self._typed_at > TYPEAHEAD_RESET_S:
            self._typed = ""
        self._typed_at = now
        self._typed += ch.lower()
        if self._typed.isdigit():                              # "1", "12": jump to that entry number
            n = int(self._typed)
            if n < len(self.options):
                self.sel = n
        else:                                                   # type a name: jump to the next entry that starts with it
            order = list(range(self.sel + (1 if len(self._typed) == 1 else 0), len(self.options))) + \
                    list(range(0, self.sel + 1))
            for i in order:
                if self.options[i].lower().startswith(self._typed):
                    self.sel = i
                    break
        event.stop()
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if self.mode == "menu" and self.options:
            row = event.y - 1 + self.top                        # 1 = first list row (below the top padding)
            if 0 <= row < len(self.options):
                self.sel = row
                self.action_activate()

    # ---- drawing ---------------------------------------------------------------------------
    def render(self) -> Text:
        w = max(20, self.size.width - 2)
        h = max(6, self.size.height - 2)
        t = Text()
        if not self.linked:
            t.append("\n  Not linked to the Core2 yet.", style=WARN)
            return t
        if self.mode == "loading":
            t.append("\n  asking the Core2 what is on its screen…", style=DIM)
            if self.error:
                t.append(f"\n  {self.error}", style=BAD)
            return t
        body_h = h - 2                                           # leave room for the status/hint rows
        if self.mode == "menu":
            n = len(self.options)
            if self.sel < self.top:
                self.top = self.sel
            elif self.sel >= self.top + body_h - 1:
                self.top = self.sel - body_h + 2
            t.append("\n")
            for i in range(self.top, min(n, self.top + body_h - 1)):
                label = self.options[i]
                if i == self.sel:
                    row = f" ▸ {i:>2}  {label}".ljust(w)
                    t.append(row + "\n", style=f"bold #000000 on {ACCENT}")
                else:
                    t.append(f"   {i:>2}", style=MUTE)
                    t.append(f"  {label}\n", style=TEXT)
            if n > body_h - 1:
                t.append(f"   ↕ {self.sel + 1}/{n}\n", style=MUTE)
        else:
            t.append("\n")
            if not self.lines:
                t.append("  (this app draws graphics only, so there is no text to show)\n", style=DIM)
            for i, line in enumerate(self.lines[:body_h - 1]):
                soft = line.strip().upper().startswith("PREV") and "NEXT" in line.upper()
                style = MUTE if soft else (f"bold {ACCENT}" if i == 0 or line.isupper() else TEXT)
                if i == 0 and len(line) < 22 and ":" in line:
                    style = DIM                                  # the status bar (clock / battery)
                t.append("  " + line + "\n", style=style)
        # status + hint rows pinned to the bottom
        used = t.plain.count("\n")
        t.append("\n" * max(0, h - used - 2))
        if self._busy_since is not None:
            t.append(" ⟳ asking the Core2…", style=WARN)
        elif self.error:
            t.append(f" ✕ {self.error}", style=BAD)
        elif self._updated:
            age = int(time.monotonic() - self._updated)
            kind = "menu" if self.mode == "menu" else "app"
            rate = "auto-refresh off" if not self.refresh_s else f"auto-refresh {self.refresh_s:g} s"
            t.append(f" {kind} · updated {age}s ago · {rate}", style=MUTE)
        t.append("\n")
        if self.mode == "menu":
            hint = " ↑↓ move · Enter open · Esc back · type a name or number to jump"
            if self._typed:
                hint = f" jump: {self._typed}▏" + " " * 4 + "(Esc back · Enter open)"
        else:
            hint = " ←/→ prev/next · Enter select · Esc back · Space refresh"
        t.append(hint, style=ACCENT_DIM)
        return t


class RefreshSlider(Widget, can_focus=True):
    """How often screen mode looks at the Core2's display by itself. It takes the command line's place."""
    BINDINGS = [
        Binding("left", "move(-1)", show=False), Binding("right", "move(1)", show=False),
        Binding("home", "set(0)", show=False), Binding("end", "set(-1)", show=False),
        Binding("tab", "focus_screen", show=False), Binding("escape", "focus_screen", show=False),
        Binding("enter", "focus_screen", show=False),
    ]

    class Changed(Message):
        def __init__(self, seconds: Optional[float]) -> None:
            super().__init__()
            self.seconds = seconds

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.idx = next(i for i, (_, v) in enumerate(REFRESH_STOPS) if v == DEFAULT_REFRESH_S)
        self.next_in: Optional[float] = None

    def on_mount(self) -> None:
        self.border_title = "Auto-refresh"
        self.set_interval(0.5, self._poll)

    def _poll(self) -> None:
        try:
            self.next_in = self.app.query_one(ScreenView).next_in()
        except Exception:
            self.next_in = None
        self.border_subtitle = ("" if REFRESH_STOPS[self.idx][1] is None or self.next_in is None
                                else f"next look in {self.next_in:.0f} s")
        self.refresh()

    def _track(self) -> tuple[int, int]:
        return 2, max(12, self.size.width - 3)

    def _stops(self) -> list[int]:
        x0, x1 = self._track()
        return [x0 + round((x1 - x0) * i / (len(REFRESH_STOPS) - 1)) for i in range(len(REFRESH_STOPS))]

    def action_move(self, d: int) -> None:
        self.action_set(max(0, min(len(REFRESH_STOPS) - 1, self.idx + d)))

    def action_set(self, i: int) -> None:
        self.idx = i % len(REFRESH_STOPS) if i >= 0 else len(REFRESH_STOPS) - 1
        self.post_message(self.Changed(REFRESH_STOPS[self.idx][1]))
        self.refresh()

    def action_focus_screen(self) -> None:
        try:
            self.app.query_one(ScreenView).focus()
        except Exception:
            pass

    def on_click(self, event: events.Click) -> None:
        self.focus()
        stops = self._stops()
        self.action_set(min(range(len(stops)), key=lambda i: abs(stops[i] - event.x)))

    def render(self) -> Text:
        w = max(20, self.size.width or 60)
        stops = self._stops()
        line = [" "] * w
        x0, x1 = self._track()
        for x in range(x0, x1 + 1):
            line[x] = "━"
        for i, x in enumerate(stops):
            line[x] = "●" if i == self.idx else "┿"
        track = Text("".join(line))
        track.stylize(ACCENT_DIM, x0, x1 + 1)
        track.stylize(f"bold {ACCENT}", stops[self.idx], stops[self.idx] + 1)
        labels = [" "] * w
        for i, (name, _) in enumerate(REFRESH_STOPS):
            pos = max(0, min(w - len(name), stops[i] - len(name) // 2))
            for k, ch in enumerate(name):
                labels[pos + k] = ch
        lab = Text("".join(labels))
        lab.stylize(MUTE)
        cur = REFRESH_STOPS[self.idx][0]
        cpos = max(0, min(w - len(cur), stops[self.idx] - len(cur) // 2))
        lab.stylize(f"bold {ACCENT}", cpos, cpos + len(cur))
        out = Text()
        out.append_text(track)
        out.append("\n")
        out.append_text(lab)
        return out
