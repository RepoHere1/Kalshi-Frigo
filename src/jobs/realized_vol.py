"""Realized volatility from the live spot feed - the honest fair value input.

The old fair value scaled a FIXED noise band by remaining time. That is why
spot could drop $50 and the contract only moved $30: the model did not know
how fast BTC was actually moving, only how fast it "usually" moves. This
module measures it from the same Coinbase prints the strategy already trades
against, per observation, and turns it into a $ sigma for any horizon:

    sigma_$ (horizon) = sigma_$ per sqrt-second * sqrt(horizon)

The trader then prices the binary as a diffusion:

    P(up) = Phi( (spot - target) / (sigma_$ * sqrt(t / 900s)) )

which is the risk-neutral probability of a driftless walk - exactly what a
15-minute settle-on-average contract converges to as time runs out. With too
few samples the module reports None and the caller keeps the legacy logistic,
so a fresh process behaves byte-for-byte like the old book until the window
fills.
"""
import math
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

# Minimum distinct observations before a sigma is published. Fewer than this
# and the standard error is a coin flip wearing a suit.
MIN_SAMPLES = 24
# Observations older than this leave the rolling window (seconds).
WINDOW_SECONDS = 45 * 60.0
# A single observation pair spanning more than this is a stale-feed gap, not
# market motion; it contributes nothing and is dropped.
MAX_PAIR_GAP_SECONDS = 90.0
# Identical consecutive prints carry no information about motion. BTC spot
# feeds can echo the last price for seconds; treating those zeros as calm
# would fake a volatility collapse and overconfident fair values.
IDENTICAL_EPSILON = 1e-9
# Sanity clamp on the published $ sigma, as FRACTIONS of spot over the
# horizon. The old absolute clamp ($5..$5000) was sized for BTC and silently
# blinded every sub-$100 asset: XRP's honest 15-minute sigma is a few tenths
# of a cent, so the $5 floor pinned its fair value at ~0.5 forever (198
# skipped_low_prob in a single log) while its market quoted a 44-point edge.
# A fraction of price is the unit-correct version: for BTC (~$81k) that is
# ~$8..$6.5k, essentially the old band; for XRP (~$1.39) ~$0.0001..$0.11,
# which lets the model speak.
MIN_SIGMA_FRACTION = 1e-4   # 1 bp of spot at the horizon
MAX_SIGMA_FRACTION = 8e-2   # 8% of spot at the horizon


class RealizedVol:
    """Rolling realized $-volatility estimated from live spot prints."""

    def __init__(self, window_seconds: float = WINDOW_SECONDS):
        self._window = float(window_seconds)
        self._samples: Deque[Tuple[float, float]] = deque()

    def observe(self, price: float, now: Optional[float] = None) -> None:
        """Record one spot print. Bad prices are ignored, never raised."""
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if not (p > 0.0) or not math.isfinite(p):
            return
        ts = float(now) if now is not None else time.monotonic()
        self._samples.append((ts, p))
        cutoff = ts - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _per_sqrt_second_sigma(self) -> Optional[float]:
        """Std of per-pair log returns scaled to $ per sqrt-second, or None.

        Pairs with a stale-feed gap or an identical print are excluded, so a
        quiet echo cannot masquerade as low volatility.
        """
        pts: List[Tuple[float, float]] = list(self._samples)
        if len(pts) < MIN_SAMPLES:
            return None
        span = pts[-1][0] - pts[0][0]
        if span <= 0.0:
            return None
        rets: List[float] = []
        gaps: List[float] = []
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0.0 or dt > MAX_PAIR_GAP_SECONDS:
                continue
            if abs(p1 - p0) <= IDENTICAL_EPSILON:
                continue
            rets.append(math.log(p1 / p0))
            gaps.append(dt)
        if len(rets) < MIN_SAMPLES:
            return None
        mean_dt = sum(gaps) / len(gaps)
        if mean_dt <= 0.0:
            return None
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1)
        sigma_per_sqrt_sec = math.sqrt(var / mean_dt)
        if not math.isfinite(sigma_per_sqrt_sec) or sigma_per_sqrt_sec <= 0.0:
            return None
        return sigma_per_sqrt_sec

    def sigma_dollars(self, horizon_seconds: float, last_price: Optional[float] = None) -> Optional[float]:
        """Expected 1-sigma $ move over `horizon_seconds`, or None if unknown.

        The estimate is anchored to the latest observed price, so the number
        is in the dollars the strategy is actually trading.
        """
        if last_price is None:
            if not self._samples:
                return None
            last_price = self._samples[-1][1]
        try:
            horizon = float(horizon_seconds)
            p = float(last_price)
        except (TypeError, ValueError):
            return None
        if horizon <= 0.0 or not (p > 0.0):
            return None
        s = self._per_sqrt_second_sigma()
        if s is None:
            return None
        out = p * s * math.sqrt(horizon)
        out = max(p * MIN_SIGMA_FRACTION, min(p * MAX_SIGMA_FRACTION, out))
        return round(out, 2)


def gaussian_cdf(z: float) -> float:
    """Standard normal CDF via erf - stdlib, no scipy on the hot path."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
