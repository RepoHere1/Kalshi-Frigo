"""Second-venue + futures truth: Binance spot, book imbalance, funding, basis.

All free, keyless, fail-open. Binance is the largest BTC venue and published
work has its mid tracking settlement oracles within ~2.5bps, so agreement
between Coinbase and Binance means Kalshi lags two independent truths, and
disagreement between the venues means wait. Nothing here raises: every fetch
returns None/0 on any failure and the ladder degrades to single-venue math.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

SPOT_URLS = [
    "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
]
DEPTH_URLS = [
    "https://data-api.binance.vision/api/v3/depth?symbol=BTCUSDT&limit=10",
    "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=10",
]
FUNDING_URLS = [
    "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1",
]
PREMIUM_URLS = [
    "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
]

_cache: Dict[str, Any] = {"at": 0.0, "spot": 0.0, "imb": 0.0, "funding": None, "basis_pct": None}
CACHE_TTL_SEC = 5.0


def _get(urls: List[str], timeout: float = 8.0) -> Optional[Any]:
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-frigo/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception:  # noqa: BLE001 - next mirror, then give up
            continue
    return None


def fetch_binance_spot() -> float:
    """BTCUSDT mid-ish last price, 0.0 on failure."""
    data = _get(SPOT_URLS)
    try:
        return float(data.get("price") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def fetch_book_imbalance() -> float:
    """Top-10 depth imbalance in percent: +100 = all bids, -100 = all asks."""
    data = _get(DEPTH_URLS)
    try:
        if not data:
            return 0.0
        # entries are [price, qty]: weight by notional, not raw size.
        bid_n = sum(float(e[0]) * float(e[1]) for e in (data.get("bids") or [])[:10])
        ask_n = sum(float(e[0]) * float(e[1]) for e in (data.get("asks") or [])[:10])
        total = bid_n + ask_n
        if total <= 0:
            return 0.0
        return (bid_n - ask_n) / total * 100.0
    except Exception:  # noqa: BLE001
        return 0.0


def fetch_futures_state() -> Tuple[Optional[float], Optional[float]]:
    """(funding_rate, basis_pct). Nones on failure (US geo-block common)."""
    funding: Optional[float] = None
    basis: Optional[float] = None
    try:
        rows = _get(FUNDING_URLS)
        if rows and isinstance(rows, list) and len(rows) > 0:
            funding = float((rows[-1].get("fundingRate") if isinstance(rows[-1], dict) else None) or 0.0)
    except Exception:  # noqa: BLE001
        funding = None
    try:
        prem = _get(PREMIUM_URLS)
        if prem and isinstance(prem, dict):
            mark = float(prem.get("markPrice") or 0.0)
            spot = float(prem.get("indexPrice") or 0.0)
            if mark > 0 and spot > 0:
                basis = (mark - spot) / spot * 100.0
    except Exception:  # noqa: BLE001
        basis = None
    return funding, basis


def snapshot() -> Dict[str, Any]:
    """Cached venue bundle: spot, imbalance, funding, basis. Never raises."""
    now = time.time()
    if now - float(_cache.get("at") or 0.0) < CACHE_TTL_SEC and _cache.get("spot"):
        return dict(_cache)
    try:
        spot = fetch_binance_spot()
        imb = fetch_book_imbalance() if spot > 0 else 0.0
        funding, basis = fetch_futures_state()
        _cache.update(
            {"at": now, "spot": spot, "imb": imb, "funding": funding, "basis_pct": basis}
        )
    except Exception:  # noqa: BLE001
        pass
    return dict(_cache)


def venue_agreement(
    coinbase: float, binance: float, target: float, max_divergence_usd: float
) -> Tuple[bool, str]:
    """Do both venues sit on the same side of the target within divergence?

    Pure logic, tested. Returns (agree, reason): disagree when the venues
    point opposite ways or are farther apart than the divergence cap. A
    missing Binance print (<=0) is 'unknown', not disagreement.
    """
    if binance <= 0 or coinbase <= 0 or target <= 0:
        return True, "single-venue (binance unavailable)"
    if abs(coinbase - binance) > max_divergence_usd:
        return False, f"venues diverge ${abs(coinbase - binance):.0f}"
    cb_side = 1 if coinbase >= target else -1
    bn_side = 1 if binance >= target else -1
    if cb_side != bn_side:
        return False, "venues straddle the strike"
    return True, "dual-venue agree"


def imbalance_sides_with(imbalance_pct: float, side: str, min_abs: float = 15.0) -> bool:
    """True unless a strong book leans against the signal side (pure)."""
    if abs(imbalance_pct) < min_abs:
        return True
    book_up = imbalance_pct > 0
    return (side == "up" and book_up) or (side == "down" and not book_up)
