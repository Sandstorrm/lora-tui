"""Keep LoRa traffic flowing (free every 0.7 s) and fire a command on the Core2's USB port; see if the link survives.
usage: repro_usbtrigger.py 'uptime' 'options 0' ..."""
import os
import sys, time
from pathlib import Path
import serial
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.__main__ import make_link, open_radio, parse_args
from bruce_lora.protocol import PROFILES
from bruce_lora.runtime import Runtime

usb = serial.Serial(os.environ.get("CORE2_PORT", "/dev/cu.usbserial-CORE2"), 115200, timeout=0.3)
args = parse_args([]); radio = open_radio(args); link = make_link(args, radio); out = bytearray()
rt = Runtime(link, radio, on_output=out.extend); rt.start()
def wait_link(limit=60):
    t = time.time()
    while link.state.value != "linked" and time.time() - t < limit: time.sleep(0.05)
    return link.state.value == "linked"
def usb_cmd(c, wait=1.5):
    usb.reset_input_buffer(); usb.write((c + "\n").encode()); time.sleep(wait); return usb.read(3000).decode(errors="replace")
for trig in sys.argv[1:]:
    if not wait_link(): print("no link; recovering Core2 module"); usb_cmd("lora reset", 7); wait_link(60)
    print(f"\n=== trigger over USB: {trig!r}  (LoRa traffic flowing, profile {PROFILES[link.profile].name}) ===")
    for _ in range(8): rt.send("free\n"); time.sleep(0.7)
    to0 = link.snapshot()["timeouts"]
    txt = usb_cmd(trig, 0.3)
    dead_at = None
    for i in range(24):
        rt.send("free\n"); time.sleep(0.5)
        if link.state.value != "linked" and dead_at is None: dead_at = (i + 1) * 0.5
    st = usb_cmd("lora status", 1.6)
    keep = [l.strip() for l in st.splitlines() if "radio timeout" in l or l.strip().startswith(("frames rx", "LoRa link", "timeouts:"))]
    print(f"  link after trigger: {'LOST after ~%.1fs' % dead_at if dead_at else 'stayed linked'}; new timeouts={link.snapshot()['timeouts']-to0}")
    print("  core2:", " | ".join(keep))
    usb_cmd("nav esc", 1.0)
    if dead_at: usb_cmd("lora reset", 7)
rt.stop()
