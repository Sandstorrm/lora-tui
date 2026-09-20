import asyncio, queue, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.engine import Link, LinkConfig
from bruce_lora.tui.app import BruceApp
from bruce_lora.tui.demo import DemoRuntime
from bruce_lora.tui.screenview import ScreenView

async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True); q = queue.SimpleQueue()
    cb = dict(on_output=lambda b: q.put(("out", b)), on_events=lambda e: q.put(("events", e)), on_snapshot=lambda s: q.put(("snap", s)))
    app = BruceApp(DemoRuntime(Link(LinkConfig(auto=False, target=4)), speed=25.0, walk=False, **cb), q, demo=True)
    async with app.run_test(size=(150, 46)) as pilot:
        sv = app.query_one(ScreenView)
        await pilot.pause(2.5); await pilot.press("f5"); await pilot.pause(2.5)
        await pilot.press("down", "down", "down", "up", "up"); app.save_screenshot(str(out / "d1_main.svg"))   # cursor on BLE
        await pilot.press("enter"); await pilot.pause(2.5)
        await pilot.press("down", "down"); app.save_screenshot(str(out / "d2_ble.svg"))                       # cursor on iBeacon
        await pilot.press("enter"); await pilot.pause(2.5)
        await pilot.press("tab"); await pilot.press("right"); await pilot.pause(0.6); app.save_screenshot(str(out / "d3_app.svg"))
    for svg in sorted(out.glob("d*.svg")):
        subprocess.run(["qlmanage", "-t", "-s", "1500", "-o", str(out), str(svg)], capture_output=True)
asyncio.run(main(Path(sys.argv[1])))
