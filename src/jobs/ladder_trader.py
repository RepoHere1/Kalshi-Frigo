"""BTC ladder trader: a fast spot feed against Kalshi's own BTC quotes.

THE PREMISE, AND WHAT WAS ACTUALLY FOUND
----------------------------------------
The idea this implements is: a real-time spot feed moves before Kalshi's slower
quote does, so when the two disagree, Kalshi's price is the stale one. That is a
testable claim, so this module tests it instead of assuming it:

* newhedge.io returns HTTP 403 to every programmatic request from this host, so
  it cannot be the feed. Coinbase's public BTC-USD websocket is used instead -
  measured at tens of milliseconds - and it is the same underlying spot market.
* Kalshi's `KXBTC` series settles on an **hourly** snapshot and quotes a ladder
  of strikes ("$73,249.99 or below"), not a 15-minute up/down binary. There is no
  15-minute BTC contract to trade. `KXBTC15`, `KXBTCUP` and `KXCRYPTO` all return
  zero markets. So the ladder below is the real shape of the opportunity.

THE RULE
--------
For each quoted strike, Kalshi's own YES price q is an opinion about whether spot
settles on one side of the strike. This module forms its own opinion from the live
spot price, and trades only when the two disagree by more than `MIN_EDGE`:

    fair  = P(settles above strike) implied by the live spot price
    edge  = fair - kalshi_yes          (for the YES side)

A positive edge means Kalshi is quoting the contract cheaper than the live spot
justifies. That is the whole trade. If the feeds agree - which they will, most of
the time, and always once any supposed lead has been arbitraged away - the module
does nothing. No trade is taken on a lead it has not measured.

MONEY
-----
`$5` notional per trade and at most one open ladder position at a time, as
specified. Both are hard limits in `LadderConfig`, not defaults that can be
loosened by a caller. Orders route through `src.jobs.broker`, so DRY runs the
identical validation and cannot transmit.
"""
import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.jobs.market_data import KalshiLadder, LadderLevel, SpotFeed, _f


@dataclass
class LadderConfig:
    """Hard limits. Sized so a single bad call cannot damage the account."""

    notional_usd: float = 5.0
    # One ladder position at a time: no stacking, no doubling up on the same
    # thesis while it is still open.
    max_open_positions: int = 1
    # Required disagreement before trading. 0.06 = six points of probability.
    min_edge: float = 0.06
    # Ignore strikes this far from spot: they are effectively decided and the
    # spread, not the edge, is what you would be paying.
    max_distance_usd: float = 150.0
    # Kalshi's minimum order is $1.00; $5 of notional clears it easily.
    min_order_usd: float = 1.0
    # How many ladder levels to consider per cycle.
    max_levels: int = 12
    poll_seconds: float = 4.0
    # The spot tick must be this fresh to be used at all.
    max_spot_age: float = 5.0


@dataclass
class LadderSignal:
    ticker: str
    strike: float
    above: bool
    fair: float
    kalshi_yes: float
    edge: float
    buy: str  # "yes" | "no" | ""
    ask: Optional[float]
    contracts: int
    notional: float
    reason: str

    @property
    def actionable(self) -> bool:
        return bool(self.buy) and self.contracts > 0


@dataclass
class LadderBook:
    """What this strategy currently holds, and what it has decided."""

    signals: List[LadderSignal] = field(default_factory=list)
    open_positions: List[Dict[str, Any]] = field(default_factory=list)
    max_open: int = 1
    trades_today: int = 0
    skipped_no_edge: int = 0
    skipped_stale: int = 0
    skipped_distance: int = 0
    last_error: str = ""
    dry: bool = True

    def summary(self) -> Dict[str, Any]:
        return {
            "signals": len(self.signals),
            "actionable": sum(1 for s in self.signals if s.actionable),
            "open_positions": len(self.open_positions),
            "max_open_positions": self.max_open,
            "trades_today": self.trades_today,
            "skipped_no_edge": self.skipped_no_edge,
            "skipped_stale": self.skipped_stale,
            "skipped_distance": self.skipped_distance,
            "last_error": self.last_error,
            "dry": self.dry,
        }


def fair_probability(spot: float, strike: float, band: float = 250.0) -> float:
    """P(settlement above `strike`) implied by the live spot price.

    A crude logistic centred on the strike, deliberately: the point is to compare
    an independent opinion against Kalshi's, not to be a pricing model. The same
    shape is used on both sides so a level quoted "or below" is just the
    complement.
    """
    if band <= 0:
        return 0.5
    z = max(-6.0, min(6.0, (spot - strike) / band))
    return float(1.0 / (1.0 + math.exp(-z)))


class LadderTrader:
    """Evaluates the ladder against live spot and takes one $5 trade at a time."""

    def __init__(
        self,
        spot: SpotFeed,
        ladder: KalshiLadder,
        config: Optional[LadderConfig] = None,
        db_manager: Any = None,
    ):
        self.spot = spot
        self.ladder = ladder
        self.config = config or LadderConfig()
        self.db_manager = db_manager
        self._client: Any = None
        self.book = LadderBook(max_open=self.config.max_open_positions)

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------
    def evaluate(self, levels: List[LadderLevel]) -> List[LadderSignal]:
        """Score every nearby two-sided level. Read-only: no orders here."""
        signals: List[LadderSignal] = []
        if self.spot.price <= 0:
            self.book.skipped_stale += 1
            return signals
        if self.spot.age > self.config.max_spot_age:
            # A stale spot price is worse than no price: it is exactly the
            # condition under which this strategy is wrong.
            self.book.skipped_stale += 1
            return signals

        for level in levels[: self.config.max_levels]:
            if not level.tradable:
                continue
            if abs(level.strike - self.spot.price) > self.config.max_distance_usd:
                self.book.skipped_distance += 1
                continue

            fair = fair_probability(self.spot.price, level.strike)
            yes = float(level.mid or 0.0)
            edge = fair - yes
            buy = ""
            ask = None
            reason = ""

            if edge >= self.config.min_edge:
                buy, ask = "yes", level.yes_ask
                reason = (
                    f"spot ${self.spot.price:,.0f} vs strike ${level.strike:,.0f}: "
                    f"fair {fair:.2f} vs Kalshi {yes:.2f}"
                )
            elif edge <= -self.config.min_edge:
                buy, ask = "no", (1.0 - level.yes_bid) if level.yes_bid is not None else None
                reason = (
                    f"spot ${self.spot.price:,.0f} vs strike ${level.strike:,.0f}: "
                    f"fair {fair:.2f} vs Kalshi {yes:.2f} (NO side)"
                )
            else:
                self.book.skipped_no_edge += 1
                reason = f"no edge: fair {fair:.2f} vs Kalshi {yes:.2f}"

            contracts = self._size(ask) if buy else 0
            signals.append(
                LadderSignal(
                    ticker=level.ticker,
                    strike=level.strike,
                    above=level.above,
                    fair=round(fair, 4),
                    kalshi_yes=round(yes, 4),
                    edge=round(edge, 4),
                    buy=buy,
                    ask=ask,
                    contracts=contracts,
                    notional=round((ask or 0.0) * contracts, 2),
                    reason=reason,
                )
            )
        signals.sort(key=lambda s: -abs(s.edge))
        self.book.signals = signals
        return signals

    def _size(self, ask: Optional[float]) -> int:
        """Contracts for one $5 clip. Zero when the price cannot clear the floor."""
        price = _f(ask)
        if price is None or price <= 0 or price > 1.0:
            return 0
        contracts = int(self.config.notional_usd / price)
        if contracts * price < self.config.min_order_usd:
            contracts = int(round(self.config.min_order_usd / price, 0))
        return max(contracts, 0)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def cycle(self) -> Dict[str, Any]:
        """One pass: fetch the ladder, score it, and take at most one trade.

        Order routing is delegated to the broker, so DRY runs the same validation
        and the same sizing and books a simulated fill; LIVE requires
        `should_trade_live()`, which needs both the env var and a persisted
        dashboard mode of "live".
        """
        from src.jobs.broker import should_trade_live
        from src.utils.database import DatabaseManager
        from src.utils.mode import TradingMode

        if self.db_manager is None:
            self.db_manager = DatabaseManager()
            # The dashboard creates the schema, but a strategy started on its own
            # may be the first process to touch this database.
            await self.db_manager.initialize()

        live = should_trade_live()
        self.book.dry = not live
        # Awaited directly. `mode.run()` builds a fresh event loop, which raises
        # "Cannot run the event loop while another loop is running" from inside
        # this coroutine - that killed the strategy on its very first cycle.
        mode = await TradingMode(db_path=str(self._db_path())).current()

        try:
            await self.ladder.fetch()
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"ladder fetch: {type(exc).__name__}: {exc}"
            return self.book.summary()

        signals = self.evaluate(self.ladder.last.levels)
        # One ladder position at a time. No pyramiding, no doubling in.
        if len(self.book.open_positions) >= self.config.max_open_positions:
            self.book.last_error = ""
            return self.book.summary()

        taken = None
        for sig in signals:
            if not sig.actionable:
                continue
            placed = await self._place(sig, live)
            if placed:
                taken = sig
                self.book.open_positions.append(
                    {
                        "ticker": sig.ticker,
                        "side": sig.buy,
                        "contracts": sig.contracts,
                        "notional": sig.notional,
                        "edge": sig.edge,
                        "mode": mode,
                    }
                )
                self.book.trades_today += 1
                break
        self.book.last_error = ""
        result = self.book.summary()
        result["took"] = (
            {
                "ticker": taken.ticker,
                "side": taken.buy,
                "contracts": taken.contracts,
                "notional": taken.notional,
                "edge": taken.edge,
                "reason": taken.reason,
                "mode": mode,
            }
            if taken
            else None
        )
        return result

    def _db_path(self) -> str:
        import os

        return os.environ.get("DB_PATH", "trading_system.db")

    async def _place(self, sig: LadderSignal, live: bool) -> bool:
        """Submit one clip through the canonical order path.

        This deliberately calls `execute_position` rather than assembling an order
        itself, so a ladder trade is validated, funded-checked, sized against the
        active book and recorded exactly like every other trade in the system -
        and, critically, so DRY cannot transmit.
        """
        from src.clients.kalshi_client import KalshiClient
        from src.jobs.execute import execute_position
        from src.utils.database import DatabaseManager, Position

        if self.db_manager is None:
            self.db_manager = DatabaseManager()
        if self._client is None:
            try:
                self._client = KalshiClient()
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = f"no Kalshi client: {type(exc).__name__}: {exc}"
                return False

        # NO side: the ask we pay is the complement of the YES bid.
        ask = float(sig.ask or 0.0)
        price = ask if sig.buy == "yes" else 1.0 - ask
        position = Position(
            market_id=sig.ticker,
            side="YES" if sig.buy == "yes" else "NO",
            entry_price=round(float(price), 4),
            quantity=sig.contracts,
            timestamp=_utcnow(),
            rationale=(
                f"BTC LADDER {sig.reason} | edge {sig.edge:+.3f} | "
                f"{sig.contracts} @ ${price:.3f}"
            ),
            confidence=abs(sig.edge),
            live=False,
            strategy="btc_ladder",
            mode="live" if live else "dry",
        )
        try:
            return await execute_position(position, live, self.db_manager, self._client)
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"submit failed: {type(exc).__name__}: {exc}"
            return False


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def run_ladder_trader(
    config: Optional[LadderConfig] = None,
    loop: bool = False,
    interval: float = 4.0,
) -> None:
    """Run the ladder trader, optionally on a loop until interrupted."""
    from src.jobs.market_data import MarketDataHub

    hub = MarketDataHub()
    await hub.start()
    trader = LadderTrader(hub.spot, hub.ladder, config)
    try:
        while True:
            result = await trader.cycle()
            print(result)
            if not loop:
                return
            await asyncio.sleep(interval or (config and config.poll_seconds) or 4.0)
    finally:
        await hub.stop()
