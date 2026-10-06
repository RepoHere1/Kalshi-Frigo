"""DRY's execution reality check: the book does not owe you your price.

The last fake in the DRY book was the fill itself: the simulator paid the ask
it saw at DECISION time, instantly, in full. LIVE pays the ask that exists
after the round trip - which can be cents worse, can be thin enough to fill
partially, and can be gone entirely. This module replays that absurdity
against the same real order book LIVE would hit:

  1. LATENCY - the fill is priced `latency_ms` after the decision, never at
     the decision.
  2. SLIPPAGE CAP - a marketable limit: if the ask has run more than
     `max_slippage` past the decision price, the order is refused, exactly
     like a capped taker order would be. Chasing a runaway ask is how a
     rehearsal burns fake money it would have burned for real.
  3. DEPTH WALK - the real orderbook is swept from the best level up to the
     cap, so thin books produce partial fills at a weighted-average price.
  4. EMPTY BOOK - no quote at execution, no fill.

Nothing here is random. Every outcome is read off live production data, so
DRY's ledger carries the same bruises LIVE's would have.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger("execution_sim")


@dataclass
class SimFill:
    """The outcome of one simulated taker execution."""

    filled: bool
    price: float = 0.0
    quantity: int = 0
    reason: str = ""


def _levels(raw: Any) -> List[Tuple[float, float]]:
    """Orderbook levels as (price_dollars, quantity), cheapest first.

    Kalshi v2 sends [[price_cents_str, qty_str], ...]; older shapes sent
    dicts. Anything unreadable is skipped - the walk just sees less depth,
    which is itself realistic.
    """
    out: List[Tuple[float, float]] = []
    if isinstance(raw, dict):
        items = raw.items()
        for k, v in items:
            try:
                out.append((float(k) / 100.0, float(v)))
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, list):
        for entry in raw:
            try:
                price_c, qty = entry[0], entry[1]
                out.append((float(price_c) / 100.0, float(qty)))
            except (TypeError, ValueError, IndexError):
                continue
    return sorted(out)


async def simulate_taker_fill(
    kalshi_client: Any,
    ticker: str,
    side: str,
    quantity: int,
    signal_price: float,
    *,
    max_slippage: float = 0.03,
    latency_ms: int = 400,
) -> SimFill:
    """Price a taker buy the way the exchange would have, `latency_ms` late.

    `side` is the position's YES/NO; `signal_price` is the ask the decision
    was priced at and anchors the slippage cap.
    """
    # 1. The round trip. The book is re-read AFTER it, never before.
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000.0)

    side_u = (side or "").upper()
    # 2. The book at execution time.
    try:
        md = await kalshi_client.get_market(ticker)
        market = (md or {}).get("market") or {}
        from src.utils.market_prices import get_market_prices

        _yb, ya, _nb, na = get_market_prices(market)
        ask = ya if side_u == "YES" else na
    except Exception as exc:  # noqa: BLE001 - an unreadable book is an unfilled order
        return SimFill(False, 0.0, 0, f"book unreadable at execution: {type(exc).__name__}")
    try:
        ask_f = float(ask) if ask is not None else 0.0
    except (TypeError, ValueError):
        ask_f = 0.0
    if ask_f <= 0.0:
        return SimFill(False, 0.0, 0, "no liquidity quoted at execution")

    cap = float(signal_price) + float(max_slippage)
    if ask_f > cap:
        return SimFill(
            False,
            0.0,
            0,
            f"slipped past cap: ask {ask_f:.3f} vs decision {float(signal_price):.3f} "
            f"(max +{float(max_slippage) * 100:.0f}c)",
        )

    # 3. Depth walk: sweep the real orderbook up to the cap.
    try:
        ob = await kalshi_client.get_orderbook(ticker)
        book = (ob or {}).get("orderbook") or ob or {}
        # Some response shapes wrap the book twice; keep unwrapping while the
        # container is a bare "orderbook" envelope with no side levels.
        while (
            isinstance(book, dict)
            and "orderbook" in book
            and "yes" not in book
            and "no" not in book
        ):
            book = book["orderbook"]
        levels = _levels(book.get("yes") if side_u == "YES" else book.get("no"))
    except Exception as exc:  # noqa: BLE001 - no depth data: fill at the touch
        return SimFill(True, round(ask_f, 4), int(quantity),
                       f"filled at the touch (depth read failed: {type(exc).__name__}: {exc})")

    if levels:
        need = int(quantity)
        remain = need
        cost = 0.0
        got = 0
        for price, size in levels:
            if price > cap or remain <= 0:
                break
            take = min(remain, int(size))
            if take <= 0:
                continue
            cost += take * price
            got += take
            remain -= take
        if got <= 0:
            return SimFill(False, 0.0, 0, "depth thinner than the slippage cap allowed")
        if got < need:
            logger.info(
                f"DRY partial fill on {ticker}: {got}/{need} contracts - "
                f"book too thin inside the cap"
            )
        return SimFill(True, round(cost / got, 4), got, "walked the book")

    # No usable depth data: the touch is the truth.
    return SimFill(True, round(ask_f, 4), int(quantity), "filled at the touch")
