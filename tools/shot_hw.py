"""Headless screenshots of the TUI on the REAL radio: python tools/shot_hw.py OUT_DIR"""
import asyncio, queue, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.__main__ import make_link, open_radio, parse_args
from bruce_lora.runtime import Runtime
from bruce_lora.tui.app import BruceApp

async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    args = parse_args([])
    q = queue.SimpleQueue()
    cb = dict(on_output=lambda b: q.put(("out", b)), on_events=lambda e: q.put(("events", e)), on_snapshot=lambda s: q.put(("snap", s)))
    radio = open_radio(args)
    app = BruceApp(Runtime(make_link(args, radio), radio, **cb), q)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(6)
        for cmd in ("uptime", "nav next", "help"):
            for ch in cmd: await pilot.press(ch)
            await pilot.press("enter"); await pilot.pause(4)
        await pilot.press("f4"); await pilot.pause(1.5)
        app.save_screenshot(str(out / "hw.svg"))
    subprocess.run(["qlmanage", "-t", "-s", "1800", "-o", str(out), str(out / "hw.svg")], capture_output=True)
asyncio.run(main(Path(sys.argv[1])))
