"""Auto range/speed: pick the fastest profile the measured link can carry with margin to spare.

Link margin of a candidate profile q, given measurements taken at some profile p:
  * RSSI does not depend on the profile:      margin_rssi(q) = rssi - sensitivity(q)
  * SNR scales with noise bandwidth:          snr_q = snr - 10*log10(bw_q / bw_p)
                                              margin_snr(q) = snr_q - snr_limit(SF_q)
The module clips its SNR report near +10 dB, so a clipped SNR carries no information and is
ignored; RSSI then decides.  The controller measures the downlink itself and the device reports
what it heard of the uplink, so the margin is the worse of the two directions.
"""
from __future__ import annotations

import math
from typing import Optional

from .protocol import MAX_POWER, N_PROFILES, PROFILES

SNR_CLIP = 9          # the RYLR998 reports SNR clipped around +10
REF_BW = 125_000
HOT_RSSI = -45.0      # dBm: TX power is lowered so the far end never hears us louder than this
MIN_POWER = 0
POWER_STEP = 3        # dB: ignore power corrections smaller than this


class AutoPolicy:
    def __init__(self, target_margin_db: float = 10.0, hysteresis_db: float = 3.0,
                 up_streak_needed: int = 4, alpha: float = 0.35):
        self.target = target_margin_db
        self.hyst = hysteresis_db
        self.up_needed = up_streak_needed
        self.alpha = alpha
        self.reset()

    def reset(self) -> None:
        self._rssi: dict[str, Optional[float]] = {"dn": None, "up": None}
        self._snr125: dict[str, Optional[float]] = {"dn": None, "up": None}
        self.up_streak = 0

    def _ema(self, old: Optional[float], new: float) -> float:
        return new if old is None else old + self.alpha * (new - old)

    def observe(self, direction: str, profile: int, rssi: float, snr: float, tx_power: int = MAX_POWER) -> None:
        """direction 'dn' = device->controller (measured here), 'up' = controller->device.

        Readings are stored as what the link would give at full TX power, so a power change made by
        the power controller does not look like a change in the path."""
        boost = MAX_POWER - tx_power
        self._rssi[direction] = self._ema(self._rssi[direction], rssi + boost)
        if snr < SNR_CLIP:
            ref = snr + boost + 10 * math.log10(PROFILES[profile].bw_hz / REF_BW)
            self._snr125[direction] = self._ema(self._snr125[direction], ref)
        else:  # clipped: it only tells us "at least this good"; keep any lower estimate
            pass

    def margin(self, q: int) -> Optional[float]:
        """Worst-direction link margin (dB) the link would have on profile q, or None if unknown."""
        p = PROFILES[q]
        worst: Optional[float] = None
        for d in ("dn", "up"):
            rssi = self._rssi[d]
            if rssi is None:
                continue
            m = rssi - p.sensitivity_dbm
            s125 = self._snr125[d]
            if s125 is not None:
                m = min(m, s125 - 10 * math.log10(p.bw_hz / REF_BW) - p.snr_limit_db)
            worst = m if worst is None else min(worst, m)
        return worst

    def suggest_power(self, direction: str, current: int) -> Optional[int]:
        """TX power that would put the far end's RSSI at HOT_RSSI (never above full power)."""
        rssi_full = self._rssi[direction]
        if rssi_full is None:
            return None
        target = round(MAX_POWER - (rssi_full - HOT_RSSI))
        target = max(MIN_POWER, min(MAX_POWER, target))
        return target if abs(target - current) >= POWER_STEP else None

    def best_profile(self, avoid: set[int] = frozenset()) -> Optional[int]:
        """Fastest profile whose margin meets the target (0 if none does); None if no data yet."""
        if self.margin(0) is None:
            return None
        ok = [q for q in range(N_PROFILES) if q not in avoid and (self.margin(q) or -99) >= self.target]
        return max(ok) if ok else 0

    def decide(self, cur: int, avoid: set[int] = frozenset()) -> int:
        best = self.best_profile(avoid)
        if best is None:
            return cur
        if best > cur:
            self.up_streak += 1
            if self.up_streak >= self.up_needed:
                self.up_streak = 0
                return best
            return cur
        self.up_streak = 0
        cur_m = self.margin(cur)
        if best < cur and cur_m is not None and cur_m < self.target - self.hyst:
            return best
        return cur
