"""Random profile hopping (with traffic) on real hardware. On a stall it dumps the controller's events and the
Core2's own `lora status` (incl. the module conversation) -- the evidence needed to see how a module went deaf.

  python tools/hw_stress_switch.py [hops] [seed]
"""
import os
import random, sys, threading, time
from pathlib import Path
import serial
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.__main__ import make_link, open_radio, parse_args
from bruce_lora.protocol import PROFILES
from bruce_lora.runtime import Runtime

CORE2 = os.environ.get("CORE2_PORT", "/dev/cu.usbserial-CORE2")
hops = int(sys.argv[1]) if len(sys.argv) > 1 else 40
rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
core2 = serial.Serial(CORE2, 115200, timeout=0.2)
core2_lines, ev_log, stop = [], [], threading.Event()
def reader():
    buf = b""
    while not stop.is_set():
        buf += core2.read(512)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1); core2_lines.append((time.time(), line.decode(errors="replace").rstrip()))
threading.Thread(target=reader, daemon=True).start()

def core2_status():
    mark = len(core2_lines); core2.write(b"lora status\n"); time.sleep(0.9)
    return [l for _, l in core2_lines[mark:]]

args = parse_args(["--profile", "0"])
radio = open_radio(args); link = make_link(args, radio)
rt = Runtime(link, radio, on_events=lambda evs: ev_log.extend((time.time(), e) for e in evs)); rt.start()
t0 = time.time()
while link.state.value != "linked" and time.time() - t0 < 60: time.sleep(0.05)
if link.state.value != "linked":
    print("NO LINK at start; Core2 says:"); print("\n".join(core2_status())); stop.set(); rt.stop(); sys.exit(2)
print(f"linked in {time.time()-t0:.1f}s; warming up 12 s so power control can settle (as it would in real use)")
rt.send("free\n"); time.sleep(12)
sn = link.snapshot(); print(f"power now: Core2 {sn['dev_power']} dBm, here {sn['ctl_power']} dBm; {hops} random hops")
stalls = 0; t_start = time.time()
for i in range(hops):
    time.sleep(1.0)                                        # a person doesn't move a slider twice a second
    p = rng.choice([x for x in range(len(PROFILES)) if x != link.profile])
    ts = time.time(); rt.set_profile(p)
    if rng.random() < 0.5: rt.send("free\n")              # traffic during the change
    probed = False
    while not (link.profile == p and link.sw is None and link.state.value == "linked") and time.time() - ts < 25:
        time.sleep(0.02)
        if not probed and time.time() - ts > 2.6:             # caught mid-failure: ask the Core2 what its module is doing
            probed = True
            snap = core2_status()
            print(f"      >>> hop {i} is slow (controller: {link.state.value} on {PROFILES[link.profile].name}); Core2 says:")
            print("\n".join("      " + l for l in snap if l.strip()))
    if not (link.profile == p and link.sw is None and link.state.value == "linked"):
        stalls += 1
        print(f"\n*** hop {i}: STALL going to {PROFILES[p].name} after {time.time()-ts:.0f}s "
              f"(controller: {link.state.value} on {PROFILES[link.profile].name}) -- run time {time.time()-t_start:.0f}s")
        for t, e in [x for x in ev_log if x[0] >= ts - 0.5][-25:]:
            if e.kind in ("tx", "rx"):
                f = e.info.get("frame"); print(f"  {t-ts:6.2f} {e.kind} {getattr(f,'kind','?')} @{PROFILES[e.info['profile']].name}")
            else: print(f"  {t-ts:6.2f} {e.kind} {e.info}")
        print("  --- Core2 `lora status`:"); print("\n".join("  " + l for l in core2_status()))
        break
    else:
        dt = time.time() - ts
        print(f"hop {i:2d} -> {PROFILES[p].name:8} ok in {dt:4.1f}s" + ("   <-- slow" if dt > 2.5 else ""), flush=True)
        if dt > 2.5:
            for t, e in [x for x in ev_log if ts - 0.3 <= x[0] <= ts + dt + 0.3]:
                if e.kind in ("tx", "rx"):
                    f = e.info.get("frame"); print(f"      {t-ts:6.2f} {e.kind} {getattr(f,'kind','?')} @{PROFILES[e.info['profile']].name}" + (f" try {e.info.get('retry')}" if e.kind == "tx" and e.info.get("retry") else ""))
                else: print(f"      {t-ts:6.2f} {e.kind} { {k: v for k, v in e.info.items() if k != 'raw'} }")
print(f"\nDONE: {i+1} hops, {stalls} stall(s), {time.time()-t_start:.0f}s")
stop.set(); rt.stop()
