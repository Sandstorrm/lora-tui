"""For each command: send it OVER LORA, see whether the link survives; recover the Core2 module over USB between trials."""
import os
import sys, time
from pathlib import Path
import serial
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.__main__ import make_link, open_radio, parse_args
from bruce_lora.protocol import PROFILES
from bruce_lora.runtime import Runtime

usb = serial.Serial(os.environ.get("CORE2_PORT", "/dev/cu.usbserial-CORE2"), 115200, timeout=0.3)
args = parse_args(["--profile", sys.argv[1]]); radio = open_radio(args); link = make_link(args, radio); out = bytearray()
rt = Runtime(link, radio, on_output=out.extend); rt.start()
def wait_link(limit=70):
    t = time.time()
    while link.state.value != "linked" and time.time() - t < limit: time.sleep(0.05)
    return link.state.value == "linked"
def usb_cmd(c, wait=1.5):
    usb.reset_input_buffer(); usb.write((c + "\n").encode()); time.sleep(wait); return usb.read(6000).decode(errors="replace")
for cmd in sys.argv[2:]:
    if not wait_link(): usb_cmd("lora reset", 7); assert wait_link(70), "cannot recover"
    time.sleep(2); out.clear(); prof = PROFILES[link.profile].name
    to0 = link.snapshot()["timeouts"]; rt.send(cmd + "\n")
    dead = None
    for i in range(9):
        time.sleep(0.5)
        if link.state.value != "linked": dead = (i + 1) * 0.5; break
    got = bytes(out)[:60].decode(errors="replace").replace("\r\n", " | ")
    print(f"{cmd!r:18} @{prof:8} -> " + (f"LINK LOST after {dead:.1f}s" if dead else "link OK") + f"   reply: {got!r}", flush=True)
    if dead:                                   # do NOT help: measure whether the Core2 heals itself
        t1 = time.time(); ok = wait_link(60)
        print(f"      self-recovery: {'link back after %.1fs' % (time.time()-t1) if ok else 'NOT recovered in 60s'}", flush=True)
        if not ok: usb_cmd("lora reset", 7)
    usb_cmd("nav esc", 1.0)
rt.stop()
