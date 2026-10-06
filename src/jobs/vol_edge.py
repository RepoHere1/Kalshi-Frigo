"""Vol-edge vs Kalshi, and the Hyperliquid perp hedge that neutralizes it.

The two-sided trade the operator asked for:

  - Kalshi's contract price INVERTED through the diffusion gives the market's
    IMPLIED volatility for the window.
  - RealizedVol gives the MEASURED volatility of the tape.
  - When implied < realized, the binary is cheap: the market pays for less
    movement than the tape actually delivers. Long the cheap side and hedge
    the direction with the BTC perp - what is left over is the vol edge.
  - The perp leg trades on Hyperliquid, whose BTC mid is public and keyless
    (verified live), so the hedge can be planned, priced and tracked whether
    or not orders are armed.

Execution honesty: placing Hyperliquid orders requires an EIP-712 wallet
signer and funded API creds. Until `HL_HEDGE_ENABLED=1` is set BY THE
OPERATOR with those creds configured, this module computes the plan - leg
sizes, direction, mark price - and logs it. DRY tracks the plan; LIVE
executes only the Kalshi leg through the standard broker path. Nothing here
sends an order anywhere on its own.
"""
import math
from typing import Optional, Tuple


def probit(p: float) -> Optional[float]:
    """Inverse standard normal CDF (Acklam's rational approximation).

    Accurate to ~1e-9 relative - far beyond the precision of a 1-cent
    contract price. None for degenerate inputs (p at or outside (0, 1),
    or exactly 0.5, where the implied sigma diverges).
    """
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if not (0.0 < p < 1.0) or abs(p - 0.5) < 1e-4:
        return None

    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def implied_sigma(
    price: float, spot: float, target: float, seconds_left: float
) -> Optional[float]:
    """The $-sigma the Kalshi price implies for this window, or None.

    Inverts P(up) = Phi((spot - target) / (sigma * sqrt(t/900s))). Prices too
    close to 0.5 (flat z) or the extremes (tape already past the target)
    carry no usable implied vol and return None.
    """
    try:
        t = max(float(seconds_left), 1.0)
        delta = float(spot) - float(target)
        p = float(price)
    except (TypeError, ValueError):
        return None
    if not (0.02 <= p <= 0.98) or abs(delta) < 1e-9:
        return None
    z = probit(p)
    if z is None or abs(z) < 1e-3:
        return None
    sigma = abs(delta) / (abs(z) * math.sqrt(t / 900.0))
    if not math.isfinite(sigma) or sigma <= 0:
        return None
    return round(sigma, 2)


def hedge_btc_qty(
    side: str,
    contracts: int,
    spot_vs_target: float,
    spot: float,
    sigma_dollars: float,
    seconds_left: float,
) -> Tuple[float, str]:
    """Perp size (BTC, signed) that flattens the binary's spot delta.

    A YES contract's dollar delta per $1 of spot is dPhi/dS = phi(z)/s with
    s = sigma * sqrt(t/900s) and z = (spot - target)/s; NO is the mirror
    image. The perp offsets it: long YES -> short BTC, long NO -> long BTC.
    Returns (qty, direction): qty in BTC (4dp) as a positive magnitude, the
    sign carried by `direction`.
    """
    try:
        n = max(int(contracts), 0)
        s = max(float(sigma_dollars), 1e-6) * math.sqrt(
            max(float(seconds_left), 1.0) / 900.0
        )
        z = float(spot_vs_target) / s
        ref = float(spot)
    except (TypeError, ValueError):
        return 0.0, ""
    if n == 0 or ref <= 0:
        return 0.0, ""
    density = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    usd_delta = n * density / s
    total_usd = usd_delta if side == "up" else -usd_delta
    # BTC clips are small at $5 notional - $0.00000084 is a real hedge size,
    # not rounding noise. Round to 8dp; the caller decides if it clears the
    # exchange minimum.
    qty = round(abs(total_usd) / ref, 8)
    direction = "short" if total_usd > 0 else "long"
    return qty, direction


class HyperliquidMark:
    """Keyless BTC perp mid from Hyperliquid's public info endpoint."""

    URL = "https://api.hyperliquid.xyz/info"

    def __init__(self) -> None:
        self.mid: Optional[float] = None
        self._updated: float = 0.0
        self.last_error: str = ""

    async def refresh(self, fetch=None) -> None:
        import time as _time

        if _time.monotonic() - self._updated < 10.0:
            return
        try:
            if fetch is None:
                import httpx

                async with httpx.AsyncClient(timeout=6.0) as client:
                    r = await client.post(
                        self.URL, json={"type": "allMids"},
                        headers={"Content-Type": "application/json"},
                    )
                    mids = r.json()
            else:
                mids = await fetch()
            raw = (mids or {}).get("BTC")
            self.mid = round(float(raw), 1) if raw else None
            self.last_error = ""
            self._updated = _time.monotonic()
        except Exception as exc:  # noqa: BLE001 - a dead feed just means no mark
            self.last_error = f"{type(exc).__name__}: {exc}"
