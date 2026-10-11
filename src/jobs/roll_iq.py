"""Roll-forward IQ: buy the NEXT window the first moment the venue lets us.

The legacy up/down scorer is anchored to whatever contract is currently
nearest expiry: at the instant the successor window is listed, its floor
strike *is* spot, the delta is zero, and the fair-value machinery has
nothing to say. Every lane therefore sits out the first minutes of every
window - exactly the minutes when the venue's price is least informed and
the book's own read of the tape is most valuable.

RollIq answers the question the scorer cannot: "is the coin going UP or
DOWN for the NEXT window?" It reuses the instruments the book already
maintains - the rolling spot ticks and the realized-vol sigma - projects
the observed drift over the next window, dilutes it through sigma, and
turns the result into P(up next window). When the successor contract
appears in the series feed with both sides quoted, the trader fires one
clip on the IQ side through the canonical order path (paper or live, like
every other clip). One clip per event: the moment the venue permits.

Pure functions are exported bare for tests; RollTracker carries the tiny
bit of per-lane state the trader keeps between cycles.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Deque, Dict, Optional, Sequence, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.jobs.market_data import UpDownMarket

# A drift read extrapolates a trend it has only observed for `lookback`
# seconds onto a full window. Full extrapolation is a lie: regimes decay.
# This damps the projection so the IQ stays a read of the tape, not a bet
# that the tape never changes.
DRIFT_EXTRAP_DAMPEN = 0.5


def window_seconds_for_series(series: str) -> Optional[float]:
    """The series' cadence from its own name: 15M -> 900s, hourly -> 3600s.

    The hourly products are the KXBTCD/KXETHD/KXXRPD 'above/below' ladders
    (Kalshi's 'D' suffix = hourly frequency). The legacy KX*1H tickers were
    phantom series, but the names still declare the cadence.
    """
    _s = (series or "").upper()
    if _s.endswith("15M"):
        return 900.0
    if _s.endswith("1H") or _s.endswith("D"):
        return 3600.0
    return None


def next_window_market(
    markets: Sequence["UpDownMarket"],
    window_sec: Optional[float],
    now: float,
    spot: Optional[float],
) -> Optional["UpDownMarket"]:
    """The successor contract, if the venue has listed it yet.

    Candidates are tradable contracts whose close sits inside
    (soonest close, soonest close + 1.25 windows]. Listings much farther out
    are open but are not *this* window's successor - rolling into the event
    5 hours away would just be holding a different book, not continuity.

    A strike ladder (many contracts share the successor close) picks the
    strike nearest the money: its quote is the first one a spot move
    reprices, and it is the only strike where an up/down read is a real
    prediction rather than an arb on a pinned 0.99/0.01 book.
    """
    if not window_sec:
        return None
    ordered = sorted(markets, key=lambda x: (x.close_ts or float("inf"),))
    soonest_close: Optional[float] = None
    candidates = []
    for m in ordered:
        if not m.tradable or not m.close_ts or m.close_ts <= now:
            continue
        if soonest_close is None:
            soonest_close = m.close_ts
            continue
        if m.close_ts > soonest_close + window_sec * 1.25:
            break
        candidates.append(m)
    if not candidates:
        return None
    if spot and spot > 0 and len(candidates) > 1:
        priced = [m for m in candidates if m.target is not None]
        if priced:
            return min(priced, key=lambda m: abs(float(m.target) - float(spot)))
    return candidates[0]


@dataclass
class RollRead:
    """One next-window read. side is "", "up" or "down"."""

    side: str
    p_up: float
    drift_frac: float
    sigma_win: Optional[float]


def drift_read(
    samples: Sequence[Tuple[float, float]],
    lookback_sec: float,
    now: float,
    window_sec: float,
    sigma_win: Optional[float],
    min_prob: float = 0.56,
) -> RollRead:
    """Project the observed drift onto the next window -> P(up).

    Uses only instruments the book already owns: the rolling spot samples
    and the realized-vol sigma at the window horizon. Returns side=""
    when the read is cold, flat, or too thin to warrant a clip.
    """
    if not samples or not window_sec:
        return RollRead("", 0.5, 0.0, sigma_win)
    samples = sorted(samples, key=lambda s: s[0])
    now_px = samples[-1][1]
    anchor = next((px for ts, px in samples if ts <= now - lookback_sec), None)
    if anchor is None or anchor <= 0 or now_px <= 0:
        return RollRead("", 0.5, 0.0, sigma_win)
    drift = (now_px - anchor) / anchor
    projected = drift * (window_sec / max(lookback_sec, 1.0)) * DRIFT_EXTRAP_DAMPEN
    if sigma_win and sigma_win > 0:
        sigma_frac = float(sigma_win) / now_px
        p_up = 0.5 * (1.0 + math.erf(projected / (sigma_frac * math.sqrt(2.0))))
    else:
        # No realized vol yet: a coarse momentum read, tightly clamped - the
        # IQ must be earned twice before it gets a clip.
        p_up = 0.5 + max(-0.20, min(0.20, projected * 3.0))
    p_up = max(0.02, min(0.98, p_up))
    if p_up >= min_prob:
        return RollRead("up", p_up, drift, sigma_win)
    if p_up <= 1.0 - min_prob:
        return RollRead("down", p_up, drift, sigma_win)
    return RollRead("", p_up, drift, sigma_win)


class RollTracker:
    """Per-lane roll state: tick history, cadence, already-bought events."""

    def __init__(self, series: str, lookback_sec: float = 300.0, sample_max: int = 600):
        self.series = series
        self.lookback_sec = lookback_sec
        self.window_sec = window_seconds_for_series(series)
        self.samples: Deque[Tuple[float, float]] = deque(maxlen=sample_max)
        # event_ticker -> {"side", "edge", "count"} of clips on this window
        self.bought: Dict[str, Dict[str, Any]] = {}

    def note(self, ts: float, price: float) -> None:
        if price and price > 0:
            self.samples.append((ts, price))

    def already(self, event_ticker: str) -> bool:
        return event_ticker in self.bought

    def clip(self, event_ticker: str) -> Dict[str, Any]:
        return self.bought.get(event_ticker, {})

    def mark(self, event_ticker: str, side: str, edge: float = 0.0) -> None:
        self.bought[event_ticker] = {"side": side, "edge": edge, "count": 1}

    def record(self, event_ticker: str, side: str, edge: float) -> None:
        """A strengthening repeat: same side only; the count grows and the
        edge keeps the best read seen. A side flip starts a fresh record -
        the trader refuses cross-side repeats before this is called."""
        cur = self.bought.get(event_ticker)
        if cur is not None and cur.get("side") == side:
            cur["count"] = int(cur.get("count", 1)) + 1
            cur["edge"] = max(float(cur.get("edge", 0.0)), float(edge))
            return
        self.mark(event_ticker, side, edge)

    def prune(self, markets: Sequence["UpDownMarket"]) -> None:
        """Forget entries for events that have rotated off the open feed."""
        open_events = {m.event_ticker for m in markets}
        for event in [k for k in self.bought if k not in open_events]:
            del self.bought[event]