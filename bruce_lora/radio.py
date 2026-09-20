"""Reyax RYLR998 over a serial port (AT commands), as used by the controller.

Blocking primitives for the runtime thread.  `AT+SEND` only answers "+OK" once the packet has
finished transmitting (measured on real modules), so send_frame() returns at end-of-TX -- exactly
what the link engine's on_tx_done() needs.
"""
from __future__ import annotations

import collections
import time
from dataclasses import dataclass
from typing import Optional

from .protocol import HOME, PROFILES


class RadioError(RuntimeError):
    pass


@dataclass
class Rx:
    data: bytes
    rssi: int
    snr: int


def parse_rcv(line: bytes) -> Optional[Rx]:
    """'+RCV=<addr>,<len>,<data>,<rssi>,<snr>' -- the payload is cut by its declared length."""
    if not line.startswith(b"+RCV="):
        return None
    try:
        a = line.index(b",", 5)
        b = line.index(b",", a + 1)
        n = int(line[a + 1:b])
        data = line[b + 1:b + 1 + n]
        tail = line[b + 1 + n:]
        if len(data) != n or not tail.startswith(b","):
            return None
        rssi, snr = tail[1:].split(b",")[:2]
        return Rx(data, int(rssi), int(snr))
    except (ValueError, IndexError):
        return None


def find_controller_port() -> Optional[str]:
    """Best guess at the controller module: a USB-UART bridge that is not busy."""
    from serial.tools import list_ports
    cands = []
    for p in list_ports.comports():
        dev = p.device
        if not dev.startswith("/dev/cu.") and not dev.upper().startswith("COM") and "ttyUSB" not in dev \
                and "ttyACM" not in dev:
            continue
        if any(x in dev.lower() for x in ("bluetooth", "debug-console", "buds", "car")):
            continue
        desc = f"{p.description} {p.manufacturer}".lower()
        score = 2 if any(k in desc for k in ("cp210", "silicon labs", "uart", "ftdi", "ch340")) else 1
        cands.append((score, dev))
    cands.sort(reverse=True)
    return cands[0][1] if cands else None


class Rylr998:
    def __init__(self, port: str, band_hz: int = 915_000_000, network: int = 18, address: int = 0,
                 power: int = 22, profile: int = HOME, baud: int = 115_200):
        import serial
        self.port = port
        self.band_hz, self.network, self.address = band_hz, network, address
        self.resets = 0
        self.ser = serial.Serial(port, baud, timeout=0.01)
        self._buf = b""
        self._rx: collections.deque[Rx] = collections.deque()
        self.profile = profile
        self.power = power
        self.at_errors = 0
        self.ser.reset_input_buffer()
        for _ in range(3):
            if self._at("AT", 0.6) == "+OK":
                break
        else:
            self.ser.close()
            raise RadioError(f"no answer from a RYLR998 on {port}")
        self._configure()
        self.version = self._at("AT+VER?", 0.6) or ""

    def _configure(self) -> None:
        for cmd in (f"AT+BAND={self.band_hz}", f"AT+NETWORKID={self.network}", f"AT+ADDRESS={self.address}",
                    f"AT+CRFOP={self.power}", f"AT+PARAMETER={PROFILES[self.profile].at_parameter}"):
            r = self._at(cmd)
            if r != "+OK":
                self.ser.close()
                raise RadioError(f"{cmd} -> {r or 'no answer'}")

    def reset(self) -> None:
        """Reset the module and restore our settings. A RYLR998 can go deaf while still answering AT commands;
        a reset is the only thing that brings its receiver back."""
        self.resets += 1
        self._rx.clear()
        self.ser.write(b"AT+RESET\r\n")
        end = time.monotonic() + 3.0
        buf = b""
        while time.monotonic() < end and b"+READY" not in buf:
            buf += self.ser.read(64)
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self._buf = b""
        self._configure()

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    # ---- line handling ---------------------------------------------------------------------
    def _pump(self, timeout: float = 0.0) -> list[str]:
        """Read what is available; queue +RCV frames, return the other lines (AT replies)."""
        others: list[str] = []
        end = time.monotonic() + timeout
        while True:
            chunk = self.ser.read(512)
            if chunk:
                self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if not line:
                    continue
                rx = parse_rcv(line)
                if rx is not None:
                    self._rx.append(rx)
                elif line.startswith(b"+RCV"):
                    self.at_errors += 1            # mangled receive line
                else:
                    others.append(line.decode("latin-1"))
            if others or self._rx or time.monotonic() >= end:
                return others

    def _at(self, cmd: str, timeout: float = 1.5) -> str:
        self.ser.write(cmd.encode() + b"\r\n")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for line in self._pump(min(0.05, max(0.0, end - time.monotonic()))):
                if line.startswith("+OK") or line.startswith("+ERR") or line.startswith("+"):
                    return line
        return ""

    # ---- operations ------------------------------------------------------------------------
    def set_profile(self, idx: int) -> bool:
        ok = self._at(f"AT+PARAMETER={PROFILES[idx].at_parameter}") == "+OK"
        if ok:
            self.profile = idx
        else:
            self.at_errors += 1
        return ok

    def set_power(self, dbm: int) -> bool:
        ok = self._at(f"AT+CRFOP={max(0, min(22, dbm))}") == "+OK"
        if ok:
            self.power = max(0, min(22, dbm))
        return ok

    def send_frame(self, data: bytes) -> bool:
        """Transmit one frame; returns when the module reports the transmission finished."""
        self.ser.write(b"AT+SEND=0,%d," % len(data) + data + b"\r\n")
        from .protocol import airtime_ms
        end = time.monotonic() + airtime_ms(self.profile, len(data)) / 1000 + 3.0
        while time.monotonic() < end:
            for line in self._pump(0.02):
                if line.startswith("+OK"):
                    return True
                if line.startswith("+ERR"):
                    self.at_errors += 1
                    return False
        self.at_errors += 1
        return False

    def poll(self, timeout: float = 0.0) -> Optional[Rx]:
        """Next received frame, waiting up to `timeout` seconds."""
        if not self._rx:
            self._pump(timeout)
        return self._rx.popleft() if self._rx else None
