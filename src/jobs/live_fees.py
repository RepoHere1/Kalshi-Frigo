"""LIVE-only helpers: fees, session filter, maker exits, 429 backoff.

DRY must never import behaviour from here on its decision path. Every helper
in this module is called exclusively from `live=True` / `live_mode=True`
branches. DRY keeps the raw 0.06 edge, instant mid fills, aggressive polling
and gross PnL exactly as before -- that book is the untouched reference.

LIVE differences, all in one place so they can be audited together:
  1. Fee math (Kalshi taker 0.07, maker ~0.0175, per-contract, rounded up).
  2. Session filter: the 18 UTC hour lost money in the forever log.
  3. Maker-first exits: rest at the quote when time allows, take when urgent.
  4. Settled-market detection: never sell into a closed/rolled book.
  5. 429 backoff with jitter + cache reuse.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone

TAKER_RATE = 0.07
MAKER_RATE = 0.0175

# LIVE funding guidance only. No code path blocks on these; they are surfaced
# on the LIVE page so the operator sizes correctly. 0.20 clip fraction means
# a $25 balance fires $5 clips but holds only one; $125 holds a full $25 book.
LIVE_MIN_BALANCE_WARN = 125.0
LIVE_RECOMMENDED_BALANCE = 200.0

# The forever log: 18 UTC went 17 trades at 41.2% for -$2.70 while neighbours
# printed. LIVE demands extra edge there; DRY is untouched and still trades it.
LOSING_HOUR_UTC = 18
LOSING_HOUR_EXTRA_EDGE = 0.03

# Maker patience: above this many seconds to settlement, LIVE rests its exit
# at the quote (maker fee). Below it, LIVE takes (same 2% discount DRY uses).
MAKER_PATIENT_SECONDS = 120.0


def taker_fee_dollars(price: float, contracts: int) -> float:
    """Taker fee for an order, in dollars. Rounded up to the cent per order."""
    p = max(0.01, min(0.99, float(price or 0.0)))
    n = max(int(contracts or 0), 0)
    if n <= 0:
        return 0.0
    total = TAKER_RATE * n * p * (1.0 - p)
    return math.ceil(total * 100 - 1e-9) / 100.0


def maker_fee_dollars(price: float, contracts: int) -> float:
    """Maker fee for a resting exit, in dollars. Rounded up per order."""
    p = max(0.01, min(0.99, float(price or 0.0)))
    n = max(int(contracts or 0), 0)
    if n <= 0:
        return 0.0
    total = MAKER_RATE * n * p * (1.0 - p)
    return math.ceil(total * 100 - 1e-9) / 100.0


def roundtrip_fee_dollars(
    entry_price: float,
    exit_price: float,
    quantity: int,
    maker_exit: bool = True,
) -> float:
    """Entry (always taker) + exit fee for one LIVE round trip."""
    qty = max(int(quantity or 0), 0)
    if qty <= 0:
        return 0.0
    entry_fee = taker_fee_dollars(entry_price, qty)
    exit_fee = maker_fee_dollars(exit_price, qty) if maker_exit else taker_fee_dollars(
        exit_price, qty
    )
    return round(entry_fee + exit_fee, 2)


def live_session_extra_edge(now: datetime | None = None) -> float:
    """Extra edge LIVE demands during the historically losing hour.

    DRY never calls this. LIVE adds the result to its required edge bar.
    """
    ts = now or datetime.now(timezone.utc)
    hour = ts.hour if ts.tzinfo is not None else ts.hour
    return LOSING_HOUR_EXTRA_EDGE if hour == LOSING_HOUR_UTC else 0.0


def should_use_maker(seconds_left: float | None) -> bool:
    """True when LIVE should rest its exit (patient), False when it must take."""
    if seconds_left is None:
        return True
    try:
        return float(seconds_left) > MAKER_PATIENT_SECONDS
    except (TypeError, ValueError):
        return True


def live_exit_limit_price(
    side: str,
    current_price: float,
    seconds_left: float | None = None,
) -> float:
    """Limit price for a LIVE exit.

    Patient (maker): rest exactly at the quote -- no 2% giveaway.
    Urgent (taker): same 2% discount DRY always uses, so urgent LIVE exits
    behave identically to DRY exits. DRY never calls this function.
    """
    px = max(float(current_price or 0.0), 0.01)
    if should_use_maker(seconds_left):
        return round(px, 4)
    return round(px * 0.98, 4)


def dry_exit_limit_price(current_price: float) -> float:
    """The historic DRY exit price. Unchanged, kept here for test parity."""
    return round(max(float(current_price or 0.0), 0.01) * 0.98, 4)


def is_settled_market(market_data: dict) -> bool:
    """True when the book has closed/rolled and no sell can fill.

    LIVE-only guard. Detects: explicit closed status, both asks >= 0.99
    (the collection-ticker shape a settled 15-min book takes), or an empty
    payload for a position old enough to have settled. DRY never calls this
    and keeps its existing attempt-and-fail path untouched.
    """
    if not isinstance(market_data, dict) or not market_data:
        return True
    status = str(market_data.get("status", "") or "").lower()
    if status == "closed":
        return True
    try:
        from src.utils.market_prices import get_market_prices

        _, yes_ask, _, no_ask = get_market_prices(market_data)
    except Exception:  # noqa: BLE001 - unreadable book reads as settled
        return True
    return bool(yes_ask >= 0.99 and no_ask >= 0.99)


def backoff_delay_seconds(attempt: int) -> float:
    """429 backoff with jitter. LIVE-only; DRY fails fast as before."""
    attempt = max(int(attempt or 0), 0)
    return round(min(2.0**attempt, 30.0) + random.uniform(0.0, 1.5), 2)
