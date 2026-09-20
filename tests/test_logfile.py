"""The session log must capture what is needed to reconstruct an incident, without drowning in idle polls."""
import logging

from bruce_lora import logfile
from bruce_lora.engine import Event
from bruce_lora.protocol import Frame


def read_log(tmp_path):
    for h in list(logfile.log.handlers):
        h.flush()
    return (tmp_path / "bruce-lora.log").read_text()


def test_log_records_state_timeouts_notices_and_data_but_not_idle_polls(tmp_path, monkeypatch):
    monkeypatch.setattr(logfile, "_path", None)
    logfile.log.handlers.clear()
    path = logfile.setup_logging(tmp_path)
    assert path == tmp_path / "bruce-lora.log" and logfile.setup_logging(tmp_path) == path     # idempotent
    idle = Frame("D", sid=3, seq=1, ack=1)
    logfile.log_event(Event("tx", 1.0, dict(raw=b"x", frame=idle, profile=0, retry=0)))
    logfile.log_event(Event("rx", 2.0, dict(raw=b"x", frame=idle, rssi=-70, snr=8, profile=0)))
    logfile.log_event(Event("tx", 3.0, dict(raw=b"x", frame=Frame("D", sid=3, payload=b"uptime\n"), profile=2, retry=1)))
    logfile.log_event(Event("rx", 4.0, dict(raw=b"garbage", frame=None, rssi=-90, snr=-2, profile=2)))
    logfile.log_event(Event("timeout", 5.0, dict(what="D", tries=3, profile=2)))
    logfile.log_event(Event("state", 6.0, dict(state="search")))
    logfile.log_event(Event("notice", 7.0, dict(text="link lost")))
    logfile.heartbeat(dict(state="linked", profile=2, auto=True, dev_power=0, ctl_power=0, dn=(-14, 8), up=(-18, 8),
                           retries=1, timeouts=1, bad=0, queued=0, at_errors=0))
    text = read_log(tmp_path)
    assert "DATA seq=1 ack=1 0B" not in text                       # idle keep-alives are left out
    assert "DATA seq=0 ack=0 7B" in text and "retry=1" in text
    assert "corrupt frame" in text and "timeout waiting for reply to D (try 3, profile 2)" in text
    assert "STATE" in text and "NOTICE link lost" in text
    assert "hb state=linked profile=2" in text and "rssi dn/up=-14/-18" in text
    logfile.log.handlers.clear()
