"""Is the truth feed alone, or is the whole market moving with it?

The operator's own observation: Coinbase spot drops $50 and Kalshi's book
follows by $20-$40 - never the same amount, because Kalshi settles on the
CF Benchmarks BRTI composite, not on one venue. A move that shows up ONLY on
Coinbase is often a one-venue dislocation; the composite the contract actually
settles on may not follow at all. A move echoed by the other major venues is
a real market-wide repricing, and the book has to chase it.

This module answers that question with two extra keyless public tickers
(Kraken, Bitstamp): how far is Coinbase from the consensus of the others, in
dollars, and does that dislocation point WITH or AGAINST the trade we are
about to make? A dislocation against the trade beyond the configured budget
vetoes the entry - the "leading indicator" was one exchange's order flow, not
the market. Fetch failures degrade to no-op; the guard never blocks on its
own outage.
"""
import asyncio
import time
from typing import Any, Dict, Optional, Tuple

import httpx

# Poll cadence and staleness. A dislocation reading older than this is not
# used: acting on a stale consensus is worse than having none.
REFRESH_SECONDS = 5.0
MAX_AGE_SECONDS = 20.0
HTTP_TIMEOUT = 4.0

_KRAKEN_URL = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
_BITSTAMP_URL = "https://www.bitstamp.net/api/v2/ticker/btcusd/"


def _kraken_last(payload: Any) -> Optional[float]:
    try:
        result = payload["result"]
        first = next(iter(result.values()))
        return float(first["c"][0])  # c = last trade closed [price, lot]
    except Exception:  # noqa: BLE001 - any schema surprise = no reading
        return None


def _bitstamp_last(payload: Any) -> Optional[float]:
    try:
        return float(payload["last"])
    except Exception:  # noqa: BLE001
        return None


class VenueDislocation:
    """Coinbase vs the consensus of other major USD venues, in dollars."""

    def __init__(self, max_dislocation_usd: float = 60.0):
        self.max_dislocation_usd = float(max_dislocation_usd)
        self._prices: Dict[str, float] = {}
        self._updated: float = 0.0
        self._lock = asyncio.Lock()
        self.last_error: str = ""

    async def refresh(self) -> None:
        """Poll both venues. One failing venue still leaves the other."""
        async with self._lock:
            now = time.monotonic()
            if now - self._updated < REFRESH_SECONDS:
                return
            fresh: Dict[str, float] = {}
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                for name, url, picker in (
                    ("kraken", _KRAKEN_URL, _kraken_last),
                    ("bitstamp", _BITSTAMP_URL, _bitstamp_last),
                ):
                    try:
                        r = await client.get(url)
                        price = picker(r.json())
                        if price and price > 0.0:
                            fresh[name] = price
                    except Exception as exc:  # noqa: BLE001 - degraded ok
                        self.last_error = f"{name}: {type(exc).__name__}"
            if fresh:
                self._prices = fresh
                self._updated = now

    def consensus(self, coinbase_spot: Optional[float] = None) -> Optional[float]:
        """Mean of the OTHER venues' last prices, or None if unavailable."""
        try:
            now = time.monotonic()
            if now - self._updated > MAX_AGE_SECONDS:
                return None
            vals = list(self._prices.values())
            if not vals:
                return None
            if coinbase_spot is not None and len(self._prices) == 1:
                # A one-venue "consensus" is just that venue. With Coinbase
                # excluded there is nothing to compare against.
                vals = [v for k, v in self._prices.items() if abs(v - float(coinbase_spot)) > 0]
            if not vals:
                return None
            return sum(vals) / len(vals)
        except Exception:  # noqa: BLE001
            return None

    def dislocation(self, coinbase_spot: Optional[float] = None) -> Optional[float]:
        """consensus - coinbase, in dollars. Positive = others are higher."""
        try:
            spot = float(coinbase_spot) if coinbase_spot is not None else None
        except (TypeError, ValueError):
            return None
        if spot is None or spot <= 0.0:
            return None
        c = self.consensus(spot)
        if c is None:
            return None
        return round(c - spot, 2)

    def vetoes(
        self, side: str, spot_vs_target: float, coinbase_spot: Optional[float] = None
    ) -> Tuple[bool, str]:
        """True when the other venues disagree with the trade beyond budget.

        `spot_vs_target` is the Coinbase distance driving the entry. If the
        OTHER venues sit on the opposite side of the target by more than the
        budget, the move is one venue's business, not the market's - and the
        settlement composite tracks the market.
        """
        try:
            delta = float(spot_vs_target)
        except (TypeError, ValueError):
            return False, ""
        if abs(delta) <= 0.0:
            return False, ""
        dis = self.dislocation(coinbase_spot)
        if dis is None:
            return False, ""
        # The move as the other venues see it: Coinbase's move minus the gap.
        others_delta = delta + dis
        # Veto only when the others are decisively on the OTHER side of the
        # target: their side of the story contradicts ours by more than the
        # budget. Small gaps are normal basis and are ignored.
        if delta > 0.0 and others_delta < -self.max_dislocation_usd:
            return True, (
                f"one-venue dislocation: others {others_delta:+,.0f} vs target "
                f"while coinbase says {delta:+,.0f} (gap {dis:+,.0f})"
            )
        if delta < 0.0 and others_delta > self.max_dislocation_usd:
            return True, (
                f"one-venue dislocation: others {others_delta:+,.0f} vs target "
                f"while coinbase says {delta:+,.0f} (gap {dis:+,.0f})"
            )
        return False, ""
