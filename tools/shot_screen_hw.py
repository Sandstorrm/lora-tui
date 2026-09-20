"""Headless TUI on the REAL radio: screen mode -> BLE -> iBeacon -> back; screenshots + timings."""
import asyncio, queue, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.__main__ import make_link, open_radio, parse_args
from bruce_lora.runtime import Runtime
from bruce_lora.tui.app import BruceApp
from bruce_lora.tui.screenview import ScreenView

async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    args = parse_args([]); q = queue.SimpleQueue()
    cb = dict(on_output=lambda b: q.put(("out", b)), on_events=lambda e: q.put(("events", e)), on_snapshot=lambda s: q.put(("snap", s)))
    radio = open_radio(args)
    app = BruceApp(Runtime(make_link(args, radio), radio, **cb), q)
    async with app.run_test(size=(150, 46)) as pilot:
        sv = app.query_one(ScreenView)
        async def wait_for(pred, what, limit=25):
            t = time.time()
            while not pred() and time.time() - t < limit: await pilot.pause(0.1)
            print(f"  {what:34} {'ok' if pred() else 'TIMEOUT'} in {time.time()-t:4.1f}s", flush=True)
        await wait_for(lambda: app.snap.get("state") == "linked", "link up", 60)
        await pilot.pause(4)
        await pilot.press("f5");            await wait_for(lambda: sv.mode == "menu", "F5 -> main menu")
        app.save_screenshot(str(out / "s1_main.svg"))
        await pilot.press("b", "enter");    await wait_for(lambda: sv.title == "Bluetooth", "type b, Enter -> BLE menu")
        await pilot.press("down", "down");  app.save_screenshot(str(out / "s2_ble.svg"))
        await pilot.press("enter");         await wait_for(lambda: sv.mode == "app", "Enter -> iBeacon app")
        await pilot.pause(1.0);             app.save_screenshot(str(out / "s3_app.svg"))
        print("  app lines:", sv.lines)
        await pilot.press("escape");        await wait_for(lambda: sv.mode == "menu", "Esc -> back at menu")
        print("  path now:", sv.path, "| link:", app.snap.get("state"))
    for svg in sorted(out.glob("s*.svg")):
        subprocess.run(["qlmanage", "-t", "-s", "1500", "-o", str(out), str(svg)], capture_output=True)
asyncio.run(main(Path(sys.argv[1])))
