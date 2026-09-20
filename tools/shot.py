"""Headless TUI screenshots (SVG -> PNG via macOS Quick Look): python tools/shot.py OUT_DIR [demo-seconds ...]"""
import asyncio
import queue
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.engine import Link, LinkConfig            # noqa: E402
from bruce_lora.tui.app import BruceApp                   # noqa: E402
from bruce_lora.tui.demo import DemoRuntime               # noqa: E402


async def main(out: Path, size=(150, 46)) -> None:
    out.mkdir(parents=True, exist_ok=True)
    q: "queue.SimpleQueue" = queue.SimpleQueue()
    cb = dict(on_output=lambda b: q.put(("out", b)), on_events=lambda e: q.put(("events", e)),
              on_snapshot=lambda s: q.put(("snap", s)))
    rt = DemoRuntime(Link(LinkConfig(auto=True)), speed=8.0, **cb)
    app = BruceApp(rt, q, demo=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(2.5)
        for ch in "help":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(4)
        app.save_screenshot(str(out / "1_near.svg"))
        await pilot.press("f4")
        await pilot.pause(9)
        app.save_screenshot(str(out / "2_mid_packets.svg"))
        await pilot.press("f2")
        await pilot.pause(0.5)
        app.save_screenshot(str(out / "3_slider_focus.svg"))
        await pilot.pause(20)
        app.save_screenshot(str(out / "4_far.svg"))
        await pilot.press("f10")
        await pilot.pause(0.5)
        app.save_screenshot(str(out / "5_help.svg"))
    for svg in sorted(out.glob("*.svg")):
        subprocess.run(["qlmanage", "-t", "-s", "1800", "-o", str(out), str(svg)], capture_output=True)

asyncio.run(main(Path(sys.argv[1])))
