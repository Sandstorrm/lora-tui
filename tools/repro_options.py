"""Send commands to the Core2 OVER LORA while capturing its USB console + link state; usage: repro_options.py CMD [CMD...]"""
import os
import sys, threading, time
from pathlib import Path
import serial
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.__main__ import make_link, open_radio, parse_args
from bruce_lora.protocol import PROFILES
from bruce_lora.runtime import Runtime

usb = serial.Serial(os.environ.get("CORE2_PORT", "/dev/cu.usbserial-CORE2"), 115200, timeout=0.2)
lines, stop = [], threading.Event()
def rd():
    buf = b""
    while not stop.is_set():
        buf += usb.read(512)
        while b"\n" in buf:
            l, buf = buf.split(b"\n", 1); lines.append((time.time(), l.decode(errors="replace").rstrip()))
threading.Thread(target=rd, daemon=True).start()

args = parse_args([]); radio = open_radio(args); link = make_link(args, radio); out = bytearray()
rt = Runtime(link, radio, on_output=out.extend); rt.start()
t0 = time.time()
while link.state.value != "linked" and time.time() - t0 < 60: time.sleep(0.05)
print(f"linked={link.state.value} after {time.time()-t0:.1f}s on {PROFILES[link.profile].name}"); time.sleep(6)
for cmd in sys.argv[1:]:
    mark, ts = len(lines), time.time(); out.clear(); rt.send(cmd + "\n")
    print(f"\n>>> {cmd}")
    for i in range(16):
        time.sleep(1.0)
        s = link.snapshot()
        print(f"  +{i+1:2d}s link={s['state']:7} {PROFILES[s['profile']].name:8} timeouts={s['timeouts']:3} out={len(out)}B", flush=True)
        if s["state"] != "linked" and i > 2: break
    print("  --- reply over LoRa:", bytes(out)[:160].decode(errors="replace").replace("\r\n", " | "))
    print("  --- Core2 USB console during this command:")
    for t, l in lines[mark:]:
        if l.strip(): print(f"    {t-ts:5.1f}s {l[:150]}")
stop.set(); rt.stop()
