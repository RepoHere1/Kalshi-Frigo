"""LIVE and DRY fee helpers: fees, session filter, maker exits, 429 backoff.

Both DRY and LIVE now pay Kalshi's actual taker fee (0.07 * price * (1-price))
so the simulated P&L matches reality. DRY_FAKE_FEES=1 (default) enables
this. DRY keeps the raw min_edge for decision count; the fee is added
on top.

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
# printed. LIVE used to demand extra edge there; it now skips the hour
# outright -- a losing hour is cheaper to sit out than to re-learn with real
# money. DRY is untouched and still trades it.
LOSING_HOUR_UTC = 18
LOSING_HOUR_EXTRA_EDGE = 0.03  # legacy constant; the skip below supersedes it

# Maker patience: above this many seconds to settlement, LIVE rests its exit
# at the quote (maker fee). Below it, LIVE takes (same 2% discount DRY uses).
MAKER_PATIENT_SECONDS = 120.0

# Maker entries: with more than this many seconds left, LIVE posts its buy at
# the side's bid (maker fee, ~1/4 of taker) and waits MAKER_ENTRY_WAIT_SECONDS
# for a fill before cancelling and falling back to the taker ask. Below the
# patience window the entry takes immediately -- a stale book is a bigger
# risk than the fee.
MAKER_ENTRY_WAIT_SECONDS = 8.0

# The settlement window is the final 60 seconds of a 15-minute bucket. Inside
# it the BRTI feed carries the accumulating windowed average -- the number the
# contract settles on -- so a fresh windowed reading plus a book that still
# disagrees is the strongest signal this strategy gets. LIVE may enter there
# (and holds to settlement); DRY keeps the historic 45s no-entry window.
SETTLEMENT_WINDOW_SECONDS = 60.0


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
    Kept for callers/tests that still ask; the hard skip below is what
    actually keeps LIVE out of the hour now.
    """
    ts = now or datetime.now(timezone.utc)
    hour = ts.hour if ts.tzinfo is not None else ts.hour
    return LOSING_HOUR_EXTRA_EDGE if hour == LOSING_HOUR_UTC else 0.0


def live_session_skip(now: datetime | None = None) -> bool:
    """True when LIVE must not open anything at all this hour.

    The 18 UTC hour lost money in the forever log (17 trades, 41.2%,
    -$2.70) while every neighbouring hour printed. Demanding extra edge
    there still paid the fee on a coin flip; skipping costs nothing.
    DRY never calls this and still trades the hour.
    """
    ts = now or datetime.now(timezone.utc)
    return ts.hour == LOSING_HOUR_UTC


def should_use_maker_entry(seconds_left: float | None) -> bool:
    """True when a LIVE entry may rest at the bid instead of taking the ask.

    Patient only when the book has time to come to us. Inside the maker
    patience window the entry takes immediately: a stale quote is a bigger
    risk than the taker fee.
    """
    if seconds_left is None:
        return False
    try:
        return float(seconds_left) > MAKER_PATIENT_SECONDS
    except (TypeError, ValueError):
        return False


def maker_entry_price(side: str, bid: float | None, ask: float | None) -> float | None:
    """The price a patient LIVE entry posts at: the side's own bid.

    Returns None when the bid is missing or degenerate -- the caller then
    takes the ask instead of resting a bad order.
    """
    try:
        b = float(bid or 0.0)
    except (TypeError, ValueError):
        return None
    if b <= 0.0 or b >= 1.0:
        return None
    try:
        a = float(ask or 0.0)
    except (TypeError, ValueError):
        a = 0.0
    if a > 0.0 and b >= a:
        return None
    return round(b, 4)


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


# Variance-commensurate sizing: how far live spot sits from the target, in
# units of the noise band. A $58 lie with minutes to close is a stronger
# convergence bet than a $16 twitch just outside the deadband, so LIVE bets
# proportionally more. DRY never calls this and keeps its fixed $5 clip.
VARIANCE_UNIT_MULTIPLE = 2.0
VARIANCE_MAX_MULTIPLE = 2.5


def variance_clip_multiplier(delta: float, noise: float = 15.0) -> float:
    """Clip multiplier from the size of Kalshi's mispricing. LIVE-only."""
    try:
        noise_val = float(noise or 15.0)
    except (TypeError, ValueError):
        noise_val = 15.0
    if noise_val <= 0:
        return 1.0
    try:
        units = abs(float(delta or 0.0)) / noise_val
    except (TypeError, ValueError):
        return 1.0
    mult = units / VARIANCE_UNIT_MULTIPLE
    return round(max(1.0, min(VARIANCE_MAX_MULTIPLE, mult)), 2)


def parse_settlement_result(payload: object, ticker: str) -> dict | None:
    """Find Kalshi's own settlement receipt for `ticker`. LIVE-only.

    Returns {"result": "yes"|"no", "fees": float} or None when no receipt
    exists. Parsing is defensive: the settlements payload shape is walked
    generically (any list of dicts, ticker under several possible keys,
    result/fees under several possible keys). No receipt ever means no
    action -- the caller must leave the position open, never invent a close.
    """
    want = str(ticker or "").strip()
    if not want or not isinstance(payload, dict):
        return None
    cands: list = []
    for value in payload.values():
        if isinstance(value, list):
            cands.extend(value)
        elif isinstance(value, dict):
            for sub in value.values():
                if isinstance(sub, list):
                    cands.extend(sub)
    for row in cands:
        if not isinstance(row, dict):
            continue
        for key in ("ticker", "market_ticker", "event_ticker"):
            if str(row.get(key) or "").strip() == want:
                break
        else:
            continue
        result = None
        for key in ("result", "market_result", "outcome", "settlement_result"):
            val = str(row.get(key) or "").strip().lower()
            if val in ("yes", "no"):
                result = val
                break
        if result is None:
            continue
        fees = 0.0
        for key in ("fees", "fee", "total_fees", "taker_fees", "fees_paid"):
            try:
                fees = abs(float(row.get(key) or 0.0))
                break
            except (TypeError, ValueError):
                continue
        return {"result": result, "fees": round(fees, 2)}
    return None
