"""Order submission with a hard DRY / LIVE split.

DRY is meant to be a genuine rehearsal, so it runs the *same* validation as
the live path runs - tradeability, Kalshi's 1-99c range, and an affordability
check - against the same live order book, and produces the same order
payload. The only thing it does not do is transmit it.

That separation is structural, not a flag:

- `DryBroker` has no `place_order` method and never receives a KalshiClient,
  so there is no code path from a DRY submission to the order endpoint.
- `LiveBroker` is the only object that holds a KalshiClient, and it is only
  constructed when the persisted mode is actually LIVE.
- `broker_for_mode` is the single place the two are chosen.

A DRY order therefore fails for every reason a live order would fail, minus the
side effect. That is the whole point: a DRY run that succeeds proves the real
path is wired up, not merely that the arithmetic works out locally.
"""
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger("broker")

MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 99
# Kalshi's minimum order size is ONE CONTRACT. The V2 order endpoint takes a
# whole-contract `count` ("count": "N.00") and has no dollar-notional floor:
# a single 1-cent contract is a valid $0.01 order. The old ``$1.00`` notional
# minimum was fabricated here and silently turned every sub-$1 clip into a
# no-op, which is what left a tiny account sitting out forever. The exchange
# minimum is expressed in contracts, not dollars.
MIN_ORDER_CONTRACTS = 1


@dataclass
class OrderRequest:
    """A validated order, ready to submit. Mode-agnostic by construction."""

    ticker: str
    client_order_id: str
    side: str
    action: str
    count: int
    type_: str = "market"
    yes_price: Optional[int] = None
    no_price: Optional[int] = None
    expiration_ts: Optional[int] = None
    # Dollars actually at risk, computed during validation.
    notional: float = 0.0
    fill_price: float = 0.0
    meta: Dict[str, Any] = None

    def as_place_order_kwargs(self) -> Dict[str, Any]:
        """Exactly the kwargs KalshiClient.place_order expects."""
        out: Dict[str, Any] = {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "action": self.action,
            "count": self.count,
            "type_": self.type_,
        }
        if self.yes_price is not None:
            out["yes_price"] = self.yes_price
        if self.no_price is not None:
            out["no_price"] = self.no_price
        if self.expiration_ts is not None:
            out["expiration_ts"] = self.expiration_ts
        return out


def minimum_viable_quantity(price_dollars: float) -> int:
    """Smallest contract count Kalshi will accept at this price.

    The exchange minimum is one contract, regardless of dollar notional (a
    1-cent contract is a valid $0.01 order). A percentage-based sizer on a
    tiny balance can produce a fraction of a contract; that rounds up to 1 so
    the order is actually a whole contract rather than silently refused.
    """
    price = float(price_dollars)
    if price <= 0:
        return 0
    # One contract is always viable; nothing fractional exists on the wire.
    return MIN_ORDER_CONTRACTS


def apply_size_floor(quantity: int, price_dollars: float) -> int:
    """Raise `quantity` to the exchange minimum if it falls short.

    Never lowers it - a larger intended size is always respected.
    """
    return max(int(quantity), minimum_viable_quantity(price_dollars))


def build_order_request(
    *,
    market_id: str,
    side: str,
    action: str,
    quantity: int,
    market: Dict[str, Any],
    available_cents: int,
    limit_price_dollars: Optional[float] = None,
) -> Any:
    """Validate a trade against the live book and build the order.

    Returns `(request, "")` on success or `(None, reason)` with a
    human-readable reason, which is what the caller logs. Every rejection here
    is a rejection Kalshi itself would have made, so DRY surfaces real
    untradable markets instead of inventing fills for them.

    `available_cents` is the funding source for *this* mode: the real Kalshi
    balance under LIVE, the simulated DRY cash under DRY.
    """
    from src.utils.market_prices import get_market_prices, is_tradeable_market

    side_lower = (side or "").lower()
    if side_lower not in ("yes", "no"):
        return None, f"invalid side {side!r}; must be 'YES' or 'NO'"
    if action not in ("buy", "sell"):
        return None, f"invalid action {action!r}; must be 'buy' or 'sell'"
    if int(quantity) <= 0:
        return None, f"invalid quantity {quantity}"

    # Guard 1 (issue #42): collection/aggregate tickers quote $1.00/$1.00 and are
    # not directly tradeable - Kalshi answers HTTP 400 invalid_price.
    if not is_tradeable_market(market):
        return None, f"{market_id}: collection/aggregate ticker is not directly tradeable"

    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market)

    # A limit price short-circuits the book; otherwise use the side's ask, which
    # is what a market order would actually pay.
    if limit_price_dollars is not None:
        price_dollars = float(limit_price_dollars)
    else:
        price_dollars = yes_ask if side_lower == "yes" else no_ask

    # Guard 2 (issue #42): Kalshi only accepts 1-99c.
    price_cents = int(round(price_dollars * 100))
    if not (MIN_PRICE_CENTS <= price_cents <= MAX_PRICE_CENTS):
        return None, (
            f"{market_id}: {side_lower} price {price_dollars:.4f} -> {price_cents}c "
            f"is outside Kalshi's valid range {MIN_PRICE_CENTS}-{MAX_PRICE_CENTS}c"
        )

    notional_cents = price_cents * int(quantity)

    # Guard 3: never send a buy the funding source cannot cover.
    # Sells return cash, so they are never blocked by available balance.
    if action == "buy" and notional_cents > int(available_cents):
        return None, (
            f"{market_id}: needs {notional_cents}c "
            f"({quantity} @ {price_cents}c) but only {int(available_cents)}c available"
        )

    # Size floor: raise to one whole contract (the exchange minimum), but only
    # if we can afford it. Applies to buys; a sell of a small remainder should
    # be left alone so the position can actually be closed out.
    if action == "buy":
        floored = apply_size_floor(int(quantity), price_dollars)
        if floored != int(quantity):
            if floored * price_cents > int(available_cents):
                return None, (
                    f"{market_id}: reaching Kalshi's 1-contract minimum needs "
                    f"{floored} @ {price_cents}c = {floored * price_cents}c, "
                    f"but only {int(available_cents)}c available"
                )
            logger.info(
                f"Rounding {quantity} -> {floored} contracts: {quantity} @ "
                f"{price_cents}c is a fractional contract, under Kalshi's "
                f"1-contract minimum order size"
            )
            quantity = floored
            notional_cents = price_cents * quantity

    req = OrderRequest(
        ticker=market_id,
        client_order_id=str(uuid.uuid4()),
        side=side_lower,
        action=action,
        count=int(quantity),
        type_="limit" if limit_price_dollars is not None else "market",
        notional=notional_cents / 100.0,
        fill_price=price_dollars,
        meta={
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
        },
    )
    if side_lower == "yes":
        req.yes_price = price_cents
    else:
        req.no_price = price_cents
    return req, ""


def _simulated_response(req: OrderRequest) -> Dict[str, Any]:
    """A Kalshi-shaped order response for a fill that never left the box."""
    return {
        "order": {
            "order_id": f"dry-{req.client_order_id}",
            "client_order_id": req.client_order_id,
            "ticker": req.ticker,
            "side": req.side,
            "action": req.action,
            "count": req.count,
            "type": req.type_,
            "status": "filled",
            "yes_price": req.yes_price,
            "no_price": req.no_price,
            "create_time": datetime.now(timezone.utc).isoformat(),
        },
        "simulated": True,
    }


class DryBroker:
    """The rehearsal book.

    It holds NO KalshiClient and exposes NO place_order, so there is no code
    path from here to the order endpoint. Its cash is the DRY book's derived
    balance (see TradingMode.dry_account()), so an affordability refusal here
    is the same refusal LIVE would give, measured against rehearsal money.
    """

    simulated = True

    def __init__(self, mode_manager: Any):
        self._mode = mode_manager

    async def available_cents(self) -> int:
        account = await self._mode.dry_account()
        return int(round(float(account.get("cash") or 0.0) * 100))

    async def submit(self, req: OrderRequest) -> Dict[str, Any]:
        """Validate, then simulate the fill. Nothing leaves the process."""
        logger.info(
            f"DRY order simulated: {req.action} {req.count} {req.side.upper()} "
            f"@ {req.fill_price:.3f} on {req.ticker} "
            f"(notional ${req.notional:.2f})"
        )
        return _simulated_response(req)

    async def cancel(self, order_id: str) -> Dict[str, Any]:
        # A simulated order was never transmitted, so there is nothing to
        # cancel on the exchange - but reporting success keeps every caller's
        # control flow identical to LIVE's.
        return {"order": {"order_id": order_id, "status": "cancelled"}}


class LiveBroker:
    """The only object that can reach the Kalshi order endpoint."""

    simulated = False

    def __init__(self, kalshi_client: Any):
        self._client = kalshi_client

    async def available_cents(self) -> int:
        balance = await self._client.get_balance()
        cents: int = int(balance.get("balance", 0) or 0)
        return cents

    async def submit(self, req: OrderRequest) -> Dict[str, Any]:
        response: Dict[str, Any] = await self._client.place_order(**req.as_place_order_kwargs())
        logger.info(
            f"LIVE order submitted: {req.action} {req.count} {req.side.upper()} "
            f"@ {req.fill_price:.3f} on {req.ticker} "
            f"(notional ${req.notional:.2f})"
        )
        return response

    async def cancel(self, order_id: str) -> Dict[str, Any]:
        cancelled: Dict[str, Any] = await self._client.cancel_order(order_id)
        return cancelled


def should_trade_live() -> bool:
    """True only when live trading is permitted by BOTH switches.

    Three strategies gated their order placement on
    `settings.trading.live_trading_enabled`, which reads the LIVE_TRADING_ENABLED
    environment variable and knows nothing about the dashboard's persisted mode.
    That meant the DRY/LIVE switch on the page did not actually govern them: the
    safety came from an unrelated env var, and flipping the dashboard to LIVE
    would have left them in DRY while the page claimed otherwise.

    Two switches govern, in this order:

    1. The dashboard's persisted mode. It is the operator's current
       intent and always wins: if the page reads DRY, nothing anywhere
       trades live, whatever this process was armed into.
    2. The book this process was armed into. The dashboard stamps
       STRATEGY_BOOK_MODE on every child it spawns and scrubs
       LIVE_TRADING_ENABLED from the child env, so a spawned child must
       not read that var - it was removed precisely so a stale parent
       value could not arm a child into LIVE behind the operator's back.
       The stamp is the child's own book.

    A process with no stamp (a manual `python cli.py run --live`) falls
    back to the env var, so both switches must still agree.
    """
    import os

    from src.utils.database import _resolve_current_mode

    db_path = os.environ.get("DB_PATH", "trading_system.db")
    if _resolve_current_mode(db_path) != "live":
        return False
    book = os.environ.get("STRATEGY_BOOK_MODE", "").strip().lower()
    if book in ("dry", "live", "paper"):
        return book == "live"
    return os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"


def broker_for_mode(mode: str, *, kalshi_client: Any = None, mode_manager: Any = None) -> Any:
    """Pick the broker. The single decision point for DRY vs LIVE.

    A KalshiClient is only ever handed to LiveBroker, so constructing a broker
    for DRY cannot leak a client into a code path that could transmit.
    """
    from src.utils.mode import MODE_DRY, MODE_LIVE

    if mode == MODE_LIVE:
        if kalshi_client is None:
            raise ValueError("LIVE broker requires a KalshiClient")
        return LiveBroker(kalshi_client)
    if mode == MODE_DRY:
        if mode_manager is None:
            raise ValueError("DRY broker requires a TradingMode manager")
        return DryBroker(mode_manager)
    raise ValueError(f"unsupported mode {mode!r}; expected one of ('dry', 'live')")


async def current_mode(db_path: Optional[str] = None) -> str:
    """The persisted mode, for callers with no mode parameter of their own."""
    import os

    from src.utils.database import _resolve_current_mode

    if db_path is not None:
        path = db_path
    else:
        path = os.environ.get("DB_PATH") or "trading_system.db"
    return _resolve_current_mode(path)


def default_mode_manager(db_path: str) -> Any:
    """The TradingMode manager for this database path."""
    from src.utils.mode import TradingMode

    return TradingMode(db_path=db_path)