"""Persistent session log, so an incident ("lost the Core2 — searching…") can be reconstructed afterwards.

Rotating file (5 MB x 5) in ~/Library/Logs/bruce-lora (macOS) or ~/.local/state/bruce-lora. Records link
state changes and their reasons, timeouts, profile/power changes, radio errors/resets, every command you
send, non-idle frames, and a heartbeat every 30 s with the link's vital signs.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("bruce_lora")
_path: Optional[Path] = None


def default_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "bruce-lora"
    return Path.home() / ".local" / "state" / "bruce-lora"


def setup_logging(directory: Optional[Path] = None) -> Path:
    """Idempotent. Returns the log file path."""
    global _path
    if _path is not None:
        return _path
    d = Path(directory) if directory else default_dir()
    d.mkdir(parents=True, exist_ok=True)
    _path = d / "bruce-lora.log"
    h = logging.handlers.RotatingFileHandler(_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return _path


def fmt_frame(f) -> str:
    k = f.kind
    if k == "D":
        fl = "".join(c for c, b in (("M", 1), ("S", 2), ("F", 4)) if f.flags & b)
        return f"DATA seq={f.seq:X} ack={f.ack:X} {len(f.payload)}B{' [' + fl + ']' if fl else ''}"
    return {"H": lambda: f"HELLO sid={f.sid:X} {'fresh' if f.fresh else 'resume'}",
            "W": lambda: f"WELCOME cur={f.cur} want={f.want:X} power={f.power} {'resumed' if f.res else 'new'}",
            "P": lambda: f"SET-PROFILE ->{f.target}", "Q": lambda: f"PROFILE-OK {f.target}",
            "T": lambda: f"SET-POWER ->{f.power}", "U": lambda: f"POWER-OK {f.power}",
            "N": lambda: "NO-SESSION"}[k]()


def log_event(ev) -> None:
    """One link-engine event -> one log line (idle keep-alive polls are left out: they only bloat it)."""
    i, k = ev.info, ev.kind
    if k in ("tx", "rx"):
        f = i.get("frame")
        if f is None:
            log.warning("rx corrupt frame %dB rssi=%s snr=%s raw=%r", len(i["raw"]), i["rssi"], i["snr"], i["raw"][:60])
        elif f.kind == "D" and not f.payload and not f.flags:
            return
        elif k == "tx":
            log.debug("tx p%d %s%s", i["profile"], fmt_frame(f), f" retry={i['retry']}" if i.get("retry") else "")
        else:
            log.debug("rx p%d %s rssi=%s snr=%s", i["profile"], fmt_frame(f), i["rssi"], i["snr"])
    elif k == "timeout":
        log.warning("timeout waiting for reply to %s (try %d, profile %d)", i["what"], i["tries"], i["profile"])
    elif k == "notice":
        log.warning("NOTICE %s", i["text"])
    else:
        log.info("%s %s", k.upper(), {a: b for a, b in i.items() if a != "raw"})


def heartbeat(s: dict) -> None:
    log.info("hb state=%s profile=%s auto=%s power dev/ctl=%s/%s rssi dn/up=%s/%s retries=%s timeouts=%s bad=%s "
             "queued=%s at_errors=%s", s.get("state"), s.get("profile"), s.get("auto"), s.get("dev_power"),
             s.get("ctl_power"), (s.get("dn") or (None,))[0], (s.get("up") or (None,))[0], s.get("retries"),
             s.get("timeouts"), s.get("bad"), s.get("queued"), s.get("at_errors"))
