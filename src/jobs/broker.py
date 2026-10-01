"""Order submission with a hard DRY / LIVE split.

DRY is meant to be a genuine rehearsal, so it runs the *same* validation the
live path runs - tradeability, Kalshi's 1-99c range, and an affordability check
- against the same live order book, and produces the same order payload. The
only thing it does not do is transmit it.

That separation is structural, not a flag:

- ``DryBroker`` has no ``place_order`` method and never receives a KalshiClient,
  so there is no code path from a DRY submission to the order endpoint.
- ``LiveBroker`` is the only object that holds a KalshiClient, and it is only
  constructed when the persisted mode is actually LIVE.
- ``broker_for_mode`` is the single place the two are chosen.

A DRY order therefore fails for every reason a live order would fail, minus the
side effect. That is the whole point: a DRY run that succeeds proves the real
path is wired up, not merely that the arithmetic works out locally.
"""
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger("broker")

MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 99
# Kalshi's minimum order size. Anything smaller is rejected by the exchange.
MIN_ORDER_CENTS = 100


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
    meta: Dict[str, Any] = field(default_factory=dict)

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

    Kalshi rejects any order with a notional under $1.00. A percentage-based
    sizer can easily produce less than that - 3% of a $28 balance is $0.85 -
    which means the order is rejected by the exchange and the trade silently
    never happens. This rounds up to the smallest count that clears the floor,
    so sizing targets are achievable rather than nominal.
    """
    price = float(price_dollars)
    if price <= 0:
        return 0
    raw = MIN_ORDER_CENTS / 100.0 / price
    return max(1, int(math.ceil(raw - 1e-9)))


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
) -> Tuple[Optional[OrderRequest], str]:
    """Validate a trade against the live book and build the order.

    Returns ``(request, "")`` on success or ``(None, reason)`` with a
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

    # Guard 3: never send an order the funding source cannot cover. This is
    # checked BEFORE the size floor so a bump past the minimum can still fail
    # here rather than being submitted and rejected by the exchange.
    notional_cents = price_cents * int(quantity)
    if notional_cents > int(available_cents):
        return None, (
            f"{market_id}: needs {notional_cents}c "
            f"({quantity} @ {price_cents}c) but only {int(available_cents)}c available"
        )

    # Size floor: raise to the exchange minimum, but only if we can afford it.
    # Applies to buys; a sell of a small remainder should be left alone so the
    # position can actually be closed out.
    if action == "buy":
        floored = apply_size_floor(int(quantity), price_dollars)
        if floored != int(quantity):
            if floored * price_cents > int(available_cents):
                return None, (
                    f"{market_id}: reaching Kalshi's $1.00 minimum needs "
                    f"{floored} @ {price_cents}c = {floored * price_cents}c, "
                    f"but only {int(available_cents)}c available"
                )
            logger.info(
                f"Rounding {quantity} -> {floored} contracts: {quantity} @ "
                f"{price_cents}c is ${notional_cents / 100:.2f}, under Kalshi's "
                f"$1.00 minimum order size"
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
    """Validates and books a fill locally. Cannot transmit anything.

    Note what is absent: there is no KalshiClient here, and no place_order.
    Submissions are recorded against the DRY cash ledger so the simulated
    account reconciles with the trade history the dashboard charts.

    These methods are async and the trading loop is async, so the ledger calls
    are awaited directly. Routing them through src.utils.mode.run() would try to
    start a second event loop inside the running one and raise.
    """

    simulated = True

    def __init__(self, mode_manager: Any):
        self._mode = mode_manager

    async def available_cents(self) -> int:
        account = await self._mode.dry_account()
        return int(round(float(account["cash"]) * 100))

    async def submit(self, req: OrderRequest) -> Dict[str, Any]:
        from src.utils.mode import ModeError

        try:
            result = await self._mode.record_fill(
                market_id=req.ticker,
                side=req.side,
                action=req.action,
                quantity=req.count,
                price=req.fill_price,
                note=f"dry {req.type_} order {req.client_order_id[:8]}",
            )
        except ModeError as exc:
            # Affordability can move between validation and booking.
            return {"error": str(exc), "simulated": True}
        logger.info(
            f"DRY fill: {req.action} {req.count} {req.side.upper()} @ "
            f"{req.fill_price:.3f} on {req.ticker} - cash now ${result['cash']:.2f}"
        )
        return _simulated_response(req)

    async def cancel(self, order_id: str) -> Dict[str, Any]:
        """DRY orders are filled immediately, so there is nothing resting."""
        return {"order_id": order_id, "status": "filled", "simulated": True}


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

    Requiring both is fail-safe in the direction that matters - the dashboard
    saying DRY always wins, so no env var mistake can start trading behind the
    operator's back.
    """
    import os

    from src.utils.database import _resolve_current_mode

    env_allows = os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
    if not env_allows:
        return False
    db_path = os.environ.get("DB_PATH", "trading_system.db")
    return _resolve_current_mode(db_path) == "live"


def broker_for_mode(mode: str, *, kalshi_client: Any = None, mode_manager: Any = None) -> Any:
    """Pick the broker. The single decision point for DRY vs LIVE.

    A KalshiClient is only ever handed to LiveBroker, so constructing a broker
    for DRY cannot leak a client into a code path that could transmit.
    """
    from src.utils.mode import MODE_LIVE, TradingMode

    if mode == MODE_LIVE:
        if kalshi_client is None:
            raise ValueError("LIVE broker requires a KalshiClient")
        return LiveBroker(kalshi_client)
    if mode_manager is None:
        raise ValueError("DRY broker requires a TradingMode manager")
    return DryBroker(mode_manager)


async def current_mode(db_path: Optional[str] = None) -> str:
    """The persisted mode, for callers with no mode parameter of their own.

    The exit paths in track.py and the scalping strategies never had a live/paper
    flag - they simply placed orders. Reading the persisted mode here is what
    stops them from transmitting while the dashboard says DRY.
    """
    import os

    from src.utils.mode import TradingMode

    if db_path is not None:
        path = db_path
    else:
        path = os.environ.get("DB_PATH") or "trading_system.db"
    return await TradingMode(db_path=path).current()


def default_mode_manager(db_path: str) -> Any:
    from src.utils.mode import TradingMode

    return TradingMode(db_path=db_path)
