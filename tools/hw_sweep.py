"""Hardware sweep: switch through every profile on the real radios; check output integrity + timing."""
import hashlib, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bruce_lora.__main__ import make_link, open_radio, parse_args
from bruce_lora.protocol import PROFILES
from bruce_lora.runtime import Runtime

args = parse_args(sys.argv[1:] or ["--profile", "0"])
radio = open_radio(args); link = make_link(args, radio); out = bytearray()
rt = Runtime(link, radio, on_output=out.extend); rt.start()
t0 = time.time()
while link.state.value != "linked" and time.time() - t0 < 40: time.sleep(0.05)
assert link.state.value == "linked", "no link"
print(f"linked in {time.time()-t0:.1f}s")

def run(cmd, timeout=90):
    out.clear(); t = time.time(); rt.send(cmd + "\n")
    while not out.endswith(b"# ") and time.time() - t < timeout: time.sleep(0.02)
    return bytes(out), time.time() - t

ref, dt = run("help"); ref_md5 = hashlib.md5(ref).hexdigest()[:8]
print(f"reference help: {len(ref)} B md5 {ref_md5} in {dt:.1f}s on {PROFILES[link.profile].name}")
order = [0, 1, 2, 3, 4, 5, 3, 1, 5, 0, 4, 2, 5, 0]
bad = 0
print(f"{'profile':9} {'switch':>7} {'help':>7} {'free':>6} {'B/s':>6}  ok  rssi(dn/up)  power(dev/me)  retries")
for p in order:
    ts = time.time(); rt.set_profile(p)
    while not (link.profile == p and link.sw is None and link.state.value == "linked") and time.time() - ts < 60: time.sleep(0.02)
    sw = time.time() - ts
    if link.profile != p: print(f"{PROFILES[p].name:9} SWITCH FAILED (still {PROFILES[link.profile].name})"); bad += 1; continue
    h, dth = run("help"); f, dtf = run("free")
    ok = hashlib.md5(h).hexdigest()[:8] == ref_md5 and f.startswith(b"Total heap")
    bad += not ok
    s = link.snapshot()
    print(f"{PROFILES[p].name:9} {sw:6.1f}s {dth:6.1f}s {dtf:5.1f}s {len(h)/dth:6.0f}  {'OK ' if ok else 'BAD'}  {s['dn'][0]}/{s['up'][0]}  {s['dev_power']}/{s['ctl_power']}  {s['retries']}")
rt.stop()
print("RESULT:", "ALL OK" if not bad else f"{bad} problems")
