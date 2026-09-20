# Bruce LoRa control

Remote control for a Bruce device (M5Stack Core2 + RYLR998) from this Mac, over LoRa. You get Bruce's normal CLI in a
terminal UI — the commands run on the Core2 and their output comes back — plus live link, range/speed and power controls.

```
./run.sh                     # full-screen UI, auto-detects the controller RYLR998 (USB-UART)
./run.sh --port /dev/cu.usbserial-0001
./run.sh --demo              # simulated Core2, no hardware (the virtual radio walks near → far → near)
./run.sh --plain             # line-mode terminal
./run.sh --exec uptime --exec free      # run commands, print the output, exit
.venv/bin/python -m pytest              # 130+ tests, incl. the firmware's real C++ engine
```

This is the Mac side of a pair. The Core2 side is the firmware fork
**[Sandstorrm/bruce-firmware](https://github.com/Sandstorrm/bruce-firmware)** (branch `feat/rylr998-lora-uart`, a fork of
[BruceDevices/firmware](https://github.com/BruceDevices/firmware)). It runs the other end of the link, bridges it to Bruce's
CLI, and provides the `screen view` command that screen mode uses. This UI needs that firmware on the Core2.
Checked out side by side, the firmware is `../bruce-firmware`, which the C++ test harness in `tests/harness/` expects.

Options: `--profile auto|0-5|Range…Turbo`, `--power 0-22` (pin this radio's TX power), `--band 915`, `--timeout`.

## The UI

Black, one light-blue accent. Left: the Core2's terminal (scrollback, live prompt line, syntax colouring, history, Tab
completion). Right: **Signal** (both directions, margin, history), **Range ◂▸ Speed** slider, **TX power**, a **Core2 menu**
pad (`nav prev/select/next/back`, for when you can't see its screen), **Session** counters. `F4` shows the packet log.

| key | |
|---|---|
| type + Enter | run a Bruce command on the Core2 (`help`, `uptime`, `nav next`, `ir tx …`) |
| `↑ ↓` / `Tab` | history / complete a command name |
| `Ctrl+C` | drop the Core2's pending output (a long reply you don't want to wait for) |
| `F5` | **screen mode**: the Core2's menus and apps as a keyboard view in place of the terminal (see below) |
| `F2` | slider: `← →` pick a profile, `A` auto/manual, or click a stop |
| `F3` | power: `↑ ↓` pick Core2/this radio, `← →` change, `A` auto |
| `F6 F7 F8 F9` | Core2 menu: prev · select · next · back |
| `Ctrl+T` / `Ctrl+P` | auto range/speed / auto power on-off |
| `Ctrl+R`, `Ctrl+L`, `F10`, `Ctrl+Q` | reconnect, clear, keys, quit |

Local commands start with `:` — `:profile 3|Fast|auto`, `:power dev|ctl 5`, `:power auto`, `:flush`, `:resync`, `:clear`.

## Screen mode (F5)

The terminal is swapped for the Core2's menus, driven with the keyboard. `↑↓` move a cursor *inside the TUI* (no radio
traffic), `Enter` opens the entry, `Esc` goes back, and typing a name or a number jumps to it (`b` → BLE, `12` → entry 12).
Open an app (iBeacon, say) and you get the text that is on its display instead, refreshed every 5 s; there `←/→`, `Enter`
and `Esc` act as the Core2's prev / select / back buttons. Under the hood it is the firmware command `screen view`, which
the terminal can also run: `screen view`, `screen view open N`, `screen view back|sel|next|prev`. It answers with one line
of JSON. Apps that draw only graphics (no text) show as "nothing drawn as text".

In screen mode the command line is replaced by an **auto-refresh slider** (off, 1, 2, 5, 10, 30, 60 s; default 5 s) that
sets how often the view looks at the Core2's display by itself, in menus and apps alike. `Tab` moves to it, then `← →`
change it (or click a stop; `[` and `]` work from the screen too); `F5` brings the command line back.

## What it does

* **Range ↔ speed** — six profiles from *Range* (79 B/s, the RYLR998's maximum sensitivity) to *Turbo* (900 B/s, ~16 dB
  less range). *Auto* (default) picks the fastest one the link can carry with margin to spare; move the slider here **or
  on the Core2's LoRa menu** (Range / Speed) and the other side follows.
* **Finds each other** — after a profile change, a restart of either side, a reboot or a long dropout both ends
  rendezvous on their own; the controller hunts across all profiles and resumes the session without losing anything in flight.
* **Reliable** — CRC-16 on every frame, stop-and-wait ARQ, exactly-once/in-order delivery under loss and corruption.
* **Power control** — two radios side by side at full power overload each other. Both radios lower their TX power until
  the far end hears them at a sane level, and go back to full power the moment the path needs it.

Wire format, timings and policies: **[PROTOCOL.md](PROTOCOL.md)**.

## The Core2 side

In the Bruce firmware repo (branch `feat/rylr998-lora-uart`): `src/modules/lora/LoRaLink.{h,cpp}` +
`lora_link_engine.{h,cpp}`. It starts at boot on boards built with `USE_RYLR998_VIA_UART` (Core2: Grove Port A) and
bridges LoRa to the normal CLI without disturbing USB. On the device: **LoRa → Range / Speed** (slider + max TX power).
Over USB: `lora` prints link diagnostics, `lora reset` resets the radio module, `lora restart` restarts the link.
`screen view` reports the menu or app that is on the display (also what screen mode uses).
A watchdog probes the module and resets it if it stops answering.

## Bench notes

* Radios closer than ~1 m: leave power on *auto* (the default). If you pin `--power 22` on both, expect retries.
* The controller module is found by USB-UART bridge (CP210x/FTDI/CH340). Another program holding the port shows as
  "no answer" — close it.
* `tools/shot.py DIR` renders headless screenshots of the UI; `tools/hw_sweep.py` and `tools/hw_stress_switch.py` exercise
  real radios.
