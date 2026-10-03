"""BTC 15-minute up/down trader: a fast spot feed against Kalshi's own quotes.

THE MARKET
----------
`KXBTC15M-26OCT011715-15` - "BTC price up in next 15 mins?" Resolves Yes if the
average of sixty CF Benchmarks BRTI prints in the final minute is at least the
same average measured over the previous window. Kalshi quotes Up and Down
directly, in `*_dollars` fields.

THE RULE
--------
The target price is the previous window's settlement average, so "is it up?" is
arithmetic against a number the contract already publishes. The tradeable
question is what Kalshi *charges* for that answer:

    fair_up = 1 if spot is above target by more than NOISE else the market's own
              probability, and the trade is taken only when Kalshi's quote
              disagrees with that by more than MIN_EDGE.

NOISE exists because the settlement value is a 60-second average of a composite
index, not a spot print. Without a deadband, every tick of noise would look like
an edge and the strategy would pay the spread on nothing. The deadband is the
honest part of this: inside it, no trade, regardless of how tempting the number
looks.

MONEY
-----
`$5` notional per clip and at most one open position at a time, as specified.
Both are hard limits in `UpDownConfig`. Orders route through the canonical
`execute_position` path, so DRY runs identical validation and cannot transmit.
"""
import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket


@dataclass
class UpDownConfig:
    """Hard limits. Sized so a single bad call cannot damage the account."""

    notional_usd: float = 5.0
    max_open_positions: int = 1
    # Required disagreement with Kalshi's own price before trading.
    min_edge: float = 0.06
    # Deadband around the target, in dollars. Settlement is a 60-second average
    # of a composite index, so sub-noise moves are not information.
    noise_usd: float = 15.0
    # Kalshi's minimum order is $1.00.
    min_order_usd: float = 1.0
    # Stop opening new positions this close to expiry: the settlement window is
    # the 60 seconds before close, and a fill inside it is a coin toss.
    min_seconds_left: float = 45.0
    poll_seconds: float = 4.0
    max_spot_age: float = 5.0


@dataclass
class UpDownSignal:
    ticker: str
    bucket: Optional[str]
    side: str  # "up" | "down" | ""
    target: Optional[float]
    spot: float
    spot_vs_target: float
    fair: float
    kalshi_price: Optional[float]
    edge: float
    ask: Optional[float]
    contracts: int
    notional: float
    seconds_left: Optional[float]
    reason: str

    @property
    def actionable(self) -> bool:
        return bool(self.side) and self.contracts > 0


@dataclass
class UpDownBook:
    signals: List[UpDownSignal] = field(default_factory=list)
    open_positions: List[Dict[str, Any]] = field(default_factory=list)
    max_open: int = 1
    trades_today: int = 0
    skipped_no_edge: int = 0
    skipped_stale: int = 0
    skipped_too_close: int = 0
    skipped_unquoted: int = 0
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
            "skipped_too_close": self.skipped_too_close,
            "skipped_unquoted": self.skipped_unquoted,
            "last_error": self.last_error,
            "dry": self.dry,
        }


def fair_up_probability(spot: float, target: float, noise_usd: float = 15.0) -> float:
    """P(the next window settles at or above the target), from live spot.

    A logistic in the distance past the target, in units of the noise band. At
    the target it is exactly 0.5, which is the right prior for a market that has
    not moved yet; far past it, it saturates near 1.
    """
    if noise_usd <= 0:
        return 0.5
    z = max(-6.0, min(6.0, (spot - target) / noise_usd))
    return float(1.0 / (1.0 + math.exp(-z)))


class UpDownTrader:
    """Scores the next 15-minute contract against live spot and takes one clip."""

    def __init__(
        self,
        spot: SpotFeed,
        feed: Btc15mFeed,
        config: Optional[UpDownConfig] = None,
        db_manager: Any = None,
    ):
        self.spot = spot
        self.feed = feed
        self.config = config or UpDownConfig()
        self.db_manager = db_manager
        self._client: Any = None
        self.book = UpDownBook(max_open=self.config.max_open_positions)

    def evaluate(self, market: Optional[UpDownMarket]) -> Optional[UpDownSignal]:
        """Score the next contract. Read-only: no orders here."""
        if market is None:
            self.book.skipped_unquoted += 1
            return None
        if not market.tradable or market.target is None:
            self.book.skipped_unquoted += 1
            return None
        if self.spot.price <= 0 or self.spot.age > self.config.max_spot_age:
            self.book.skipped_stale += 1
            return None
        if (market.seconds_left or 0) < self.config.min_seconds_left:
            # The settlement window is the final 60 seconds. Inside it, spot is
            # no longer leading anything.
            self.book.skipped_too_close += 1
            return None

        spot = self.spot.price
        target = float(market.target)
        delta = round(spot - target, 2)
        # Hard deadband. Settlement is the 60-second average of a composite
        # index, so inside this band spot carries no directional information at
        # all. Without an explicit refusal the logistic still produces a small
        # "edge" from pure noise, and the strategy pays the spread on nothing.
        if abs(delta) <= self.config.noise_usd:
            self.book.skipped_no_edge += 1
            signal = UpDownSignal(
                ticker=market.ticker,
                bucket=market.bucket,
                side="",
                target=target,
                spot=spot,
                spot_vs_target=delta,
                fair=0.5,
                kalshi_price=market.up_price,
                edge=0.0,
                ask=None,
                contracts=0,
                notional=0.0,
                seconds_left=round(market.seconds_left or 0.0, 1),
                reason=(
                    f"spot {spot:,.0f} is {delta:+,.0f} from target {target:,.0f} - "
                    f"inside the ${self.config.noise_usd:,.0f} noise band, no trade"
                ),
            )
            self.book.signals = [signal]
            return signal

        fair = fair_up_probability(spot, target, self.config.noise_usd)

        up_ask = market.up_price
        down_ask = market.down_price

        # Symmetric comparison: what this contract is worth to us, minus what
        # Kalshi charges for it. Both sides are a probability in [0, 1], so the
        # edge is directly comparable.
        #
        # The DOWN side is `1 - fair_up`, not `fair_up - down_ask`: if spot sits
        # $300 below the target then DOWN is nearly certain, so the edge on DOWN
        # is what we are actually being offered. Getting that backwards made the
        # strategy refuse precisely the trades it exists to take.
        up_edge = (fair - up_ask) if up_ask is not None else 0.0
        down_edge = ((1.0 - fair) - down_ask) if down_ask is not None else 0.0

        side = ""
        ask: Optional[float] = None
        kalshi: Optional[float] = None
        edge = 0.0
        if up_edge >= self.config.min_edge and up_edge >= down_edge:
            side, ask, kalshi, edge = "up", up_ask, up_ask, up_edge
        elif down_edge >= self.config.min_edge:
            side, ask, kalshi, edge = "down", down_ask, down_ask, down_edge

        if not side:
            self.book.skipped_no_edge += 1
            reason = (
                f"spot {spot:,.0f} vs target {target:,.0f} ({delta:+,.0f}): "
                f"fair {fair:.2f}, Kalshi up "
                f"{('%.2f' % up_ask) if up_ask is not None else '--'} / down "
                f"{('%.2f' % down_ask) if down_ask is not None else '--'} - inside the edge"
            )
        else:
            reason = (
                f"spot {spot:,.0f} vs target {target:,.0f} ({delta:+,.0f}): "
                f"fair {('%.2f' % (fair if side == 'up' else 1.0 - fair))} vs Kalshi "
                f"{float(kalshi or 0.0):.2f} on {side.upper()} - edge {edge:+.3f}"
            )

        contracts = self._size(ask) if side else 0
        signal = UpDownSignal(
            ticker=market.ticker,
            bucket=market.bucket,
            side=side,
            target=target,
            spot=spot,
            spot_vs_target=delta,
            fair=round(fair, 4),
            kalshi_price=round(float(kalshi), 4) if kalshi is not None else None,
            edge=round(edge, 4),
            ask=ask,
            contracts=contracts,
            notional=round(float(ask or 0.0) * contracts, 2),
            seconds_left=round(market.seconds_left or 0.0, 1),
            reason=reason,
        )
        self.book.signals = [signal]
        return signal

    def _size(self, ask: Optional[float]) -> int:
        """Contracts for one $5 clip. Zero when the price cannot clear the floor."""
        try:
            price = float(ask) if ask is not None else 0.0
        except (TypeError, ValueError):
            return 0
        if price <= 0 or price > 1.0:
            return 0
        contracts = int(self.config.notional_usd / price)
        if contracts * price < self.config.min_order_usd:
            contracts = int(round(self.config.min_order_usd / price, 0))
        return max(contracts, 0)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _db_path(self) -> str:
        import os

        return os.environ.get("DB_PATH", "").strip() or "trading_system.db"

    async def _place(self, signal: UpDownSignal, live: bool) -> bool:
        """Submit one clip through the canonical order path.

        Delegating to `execute_position` means a clip is validated, funded-checked
        and recorded exactly like every other trade in the system - and that DRY
        cannot transmit.
        """
        from src.clients.kalshi_client import KalshiClient
        from src.jobs.execute import execute_position
        from src.utils.database import DatabaseManager, Position

        if self.db_manager is None:
            self.db_manager = DatabaseManager()
            await self.db_manager.initialize()
        if self._client is None:
            try:
                self._client = KalshiClient()
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = f"no Kalshi client: {type(exc).__name__}: {exc}"
                return False

        ask = float(signal.ask or 0.0)
        price = ask if signal.side == "up" else 1.0 - ask
        position = Position(
            market_id=signal.ticker,
            side="YES" if signal.side == "up" else "NO",
            entry_price=round(price, 4),
            quantity=signal.contracts,
            timestamp=_utcnow(),
            rationale=(
                f"BTC 15M UP/DOWN {signal.reason} | {signal.contracts} @ ${price:.3f} | "
                f"{signal.seconds_left}s left"
            ),
            confidence=abs(signal.edge),
            live=False,
            strategy="btc_updown",
            mode="live" if live else "dry",
        )
        # Persist BEFORE executing. `execute_position` needs a row id to record the
        # fill, and `add_position` is also the only thing that stops a second clip
        # being bought into a market this book already holds.
        #
        # This clip used to be handed straight to `execute_position` unsaved. The
        # DRY broker "filled" it and debited the simulated ledger, and only then
        # did execute_position notice `position.id is None` and bail. No position
        # row was written, so the book still reported zero open positions, the
        # max-open guard never tripped, and the next cycle bought the same
        # contract again a few seconds later - draining the DRY account at ~$5 a
        # pass with nothing on the board to show for it.
        try:
            position_id = await self.db_manager.add_position(position)
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"position insert failed: {type(exc).__name__}: {exc}"
            return False
        if position_id is None:
            self.book.blocked = f"already holding {signal.ticker}"
            return False
        position.id = position_id

        try:
            filled = await execute_position(position, live, self.db_manager, self._client)
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"submit failed: {type(exc).__name__}: {exc}"
            filled = False

        if not filled:
            # The row exists only because execute_position needs an id to record
            # the fill against. With no fill, keeping it leaves an orphan that
            # reads as open capital the book never spent - 23 positions against
            # 12 ledger entries, and an equity figure that no longer reconciles.
            try:
                await self.db_manager.delete_position(position_id)
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = f"orphan cleanup failed: {type(exc).__name__}: {exc}"
        return filled

    async def cycle(self) -> Dict[str, Any]:
        """One pass: refresh the series, score it, take at most one trade."""
        from src.jobs.broker import should_trade_live
        from src.utils.database import DatabaseManager
        from src.utils.mode import TradingMode

        if self.db_manager is None:
            self.db_manager = DatabaseManager()
            await self.db_manager.initialize()

        live = should_trade_live()
        self.book.dry = not live
        # Awaited directly: mode.run() builds a fresh event loop, which raises
        # from inside this coroutine.
        mode = await TradingMode(db_path=self._db_path()).current()

        try:
            await self.feed.fetch()
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"series fetch: {type(exc).__name__}: {exc}"
            return self.book.summary()

        market = self.feed.nearest()
        signal = self.evaluate(market)
        self.book.last_error = ""
        # Name the guard that actually refused, so the log says why. Returning a
        # bare "no quotable contract" for a contract that is quoted but inside the
        # settlement window is exactly the kind of wrong reason that makes a log
        # useless for diagnosis.
        if signal is None:
            refusal = (
                "no contract is quoted"
                if market is None
                else (
                    f"{market.ticker} is not quoted on both sides"
                    if self.book.skipped_unquoted
                    else (
                        f"spot tick is {self.spot.age:.1f}s old"
                        if self.spot.price <= 0 or self.spot.age > self.config.max_spot_age
                        else (
                            f"{market.seconds_left:.0f}s to settlement - inside the "
                            f"{self.config.min_seconds_left:.0f}s no-entry window"
                            if (market.seconds_left or 0) < self.config.min_seconds_left
                            else "no usable reading"
                        )
                    )
                )
            )
        else:
            refusal = ""

        took: Optional[Dict[str, Any]] = None
        blocked = ""

        # The entry guard has to read persisted positions, not just the ones this
        # process remembers. `self.book.open_positions` is in-memory and starts
        # empty on every redeploy, so it happily reported "holding 0" while the
        # database held both sides of the same contract and several positions on
        # contracts that had already settled. That is how one contract ended up
        # held as YES *and* NO - a guaranteed loss of the spread - alongside three
        # positions on long-expired buckets.
        held: List[Dict[str, Any]] = []
        stale_count = 0
        if self.db_manager is not None:
            try:
                book_mode = "live" if live else "dry"
                for p in await self.db_manager.get_open_positions(mode=book_mode):
                    if (p.strategy or "") != "btc_updown":
                        continue
                    # A 15-minute contract that is not the current one has rolled:
                    # it is settled, and nothing can be done about it here. Left to
                    # position_tracking to close out, but it must not block entry.
                    if market is not None and p.market_id != market.ticker:
                        stale_count += 1
                        continue
                    held.append(
                        {
                            "ticker": p.market_id,
                            "side": p.side,
                            "notional": (p.entry_price or 0.0) * (p.quantity or 0),
                            "contracts": p.quantity,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = f"position guard: {type(exc).__name__}: {exc}"

        if stale_count:
            self.book.blocked = f"{stale_count} position(s) on settled contracts awaiting close-out"

        # One position at a time. No pyramiding, no doubling in.
        if signal is not None and signal.actionable:
            # Never hold both sides of one contract: the two payouts sum to $1.00
            # while the combined cost is whatever was paid, so the pair is a
            # certain loss of the spread.
            opposite = {"up": "NO", "down": "YES"}
            for p in held:
                if p["ticker"] == signal.ticker and p["side"] == opposite.get(signal.side):
                    blocked = (
                        f"already hold the opposite side of {signal.ticker}; "
                        "closing first (never both sides of one contract)"
                    )
                    break
                if p["ticker"] == signal.ticker:
                    blocked = f"already holding {signal.ticker}"
                    break

            if not blocked and len(held) >= self.config.max_open_positions:
                blocked = f"already holding {len(held)} open position(s); " "max is one at a time"
            elif not blocked and await self._place(signal, live):
                self.book.open_positions.append(
                    {
                        "ticker": signal.ticker,
                        "side": signal.side,
                        "contracts": signal.contracts,
                        "notional": signal.notional,
                        "edge": signal.edge,
                        "mode": mode,
                    }
                )
                self.book.trades_today += 1
                took = {
                    "ticker": signal.ticker,
                    "side": signal.side,
                    "contracts": signal.contracts,
                    "notional": signal.notional,
                    "edge": signal.edge,
                    "reason": signal.reason,
                    "mode": mode,
                }

        # One flat summary: the book counters plus this cycle's reading.
        result = self.book.summary()
        result.update(
            {
                "took": took,
                "blocked": blocked,
                "contract": market.ticker if market else None,
                "bucket": market.bucket if market else None,
                "seconds_left": (
                    round(market.seconds_left, 1)
                    if market is not None and market.seconds_left is not None
                    else None
                ),
                "target": market.target if market else None,
                "spot": round(self.spot.price, 2) if self.spot.price else None,
                "spot_vs_target": signal.spot_vs_target if signal else None,
                "fair_up": signal.fair if signal else None,
                "kalshi_up": signal.kalshi_price if signal else None,
                "reason": (signal.reason if signal else refusal) or "no quotable contract",
            }
        )
        return result


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def run_updown_trader(
    config: Optional[UpDownConfig] = None,
    loop: bool = False,
    interval: float = 0.0,
) -> None:
    """Run the 15-minute up/down trader, optionally on a loop until interrupted."""
    from src.jobs.market_data import MarketDataHub

    hub = MarketDataHub()
    await hub.start()
    trader = UpDownTrader(hub.spot, hub.feed, config)
    sleep_for = interval or (config and config.poll_seconds) or 4.0
    try:
        while True:
            result = await trader.cycle()
            print(result)
            if not loop:
                return
            await asyncio.sleep(sleep_for)
    finally:
        await hub.stop()
