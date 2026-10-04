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
    # Required disagreement with Kalshi's own price before trading.
    min_edge: float = 0.06
    # LIVE only: buy-side fee per fill, as a fraction of notional. Kalshi
    # charges on every fill, so a 6c edge that clears DRY is only a ~3c edge
    # LIVE once the fee is taken out. LIVE therefore requires
    # edge >= min_edge + live_fee_rate; DRY keeps the raw min_edge so the two
    # books stay comparable in decision count. Estimated from the account's own
    # fill history (fees against notional across hundreds of fills).
    live_fee_rate: float = 0.03
    # LIVE only: the largest fraction of the available balance one clip may
    # spend. A fixed $5 clip empties a small account in a trade or two and it
    # then sits idle all night - sizing down against the balance keeps the book
    # firing on every real edge instead of going dark after one buy.
    live_cash_fraction: float = 0.15
    # Minimum model probability on the chosen side before any entry. The old
    # "one clip per market" guard was removed; this is what replaces it as the
    # anti-churn rule - repeated buys of the same contract are allowed, but only
    # while the contract is still more likely than not to pay.
    min_win_prob: float = 0.60
    # Total simulated/live notional the book may hold open across ALL clips,
    # including multiple clips of the same contract. This replaces the old
    # max_open_positions position COUNT, which forbade a second clip of the same
    # market even when the odds justified it.
    max_open_notional: float = 25.0
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
    max_open_notional: float = 25.0
    trades_today: int = 0
    skipped_no_edge: int = 0
    skipped_stale: int = 0
    skipped_too_close: int = 0
    skipped_unquoted: int = 0
    skipped_low_prob: int = 0
    last_error: str = ""
    dry: bool = True

    def summary(self) -> Dict[str, Any]:
        return {
            "signals": len(self.signals),
            "actionable": sum(1 for s in self.signals if s.actionable),
            "open_positions": len(self.open_positions),
            "open_notional": round(sum(float(p.get("notional") or 0.0) for p in self.open_positions), 2),
            "max_open_notional": self.max_open_notional,
            "trades_today": self.trades_today,
            "skipped_no_edge": self.skipped_no_edge,
            "skipped_stale": self.skipped_stale,
            "skipped_too_close": self.skipped_too_close,
            "skipped_unquoted": self.skipped_unquoted,
            "skipped_low_prob": self.skipped_low_prob,
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
        self.book = UpDownBook(max_open_notional=self.config.max_open_notional)

    def evaluate(
        self,
        market: Optional[UpDownMarket],
        live: bool = False,
        clip_usd: Optional[float] = None,
    ) -> Optional[UpDownSignal]:
        """Score the next contract. Read-only: no orders here.

        `live` raises the edge bar by `live_fee_rate`: DRY pays no fee, LIVE
        does, and an edge that clears DRY but not the fee is a losing trade with
        real money. `clip_usd` overrides the fixed clip size (used in LIVE to
        size down against the available balance).
        """
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

        # LIVE pays the fee out of exactly this edge, so the bar is higher there.
        required = self.config.min_edge + (self.config.live_fee_rate if live else 0.0)

        side = ""
        ask: Optional[float] = None
        kalshi: Optional[float] = None
        edge = 0.0
        if up_edge >= required and up_edge >= down_edge:
            side, ask, kalshi, edge = "up", up_ask, up_ask, up_edge
        elif down_edge >= required:
            side, ask, kalshi, edge = "down", down_ask, down_ask, down_edge

        if not side:
            self.book.skipped_no_edge += 1
            reason = (
                f"spot {spot:,.0f} vs target {target:,.0f} ({delta:+,.0f}): "
                f"fair {fair:.2f}, Kalshi up "
                f"{('%.2f' % up_ask) if up_ask is not None else '--'} / down "
                f"{('%.2f' % down_ask) if down_ask is not None else '--'} - "
                f"best edge {max(up_edge, down_edge):+.3f} under {required:.3f}"
                + (" (fee-aware)" if live else "")
            )
        else:
            reason = (
                f"spot {spot:,.0f} vs target {target:,.0f} ({delta:+,.0f}): "
                f"fair {('%.2f' % (fair if side == 'up' else 1.0 - fair))} vs Kalshi "
                f"{float(kalshi or 0.0):.2f} on {side.upper()} - edge {edge:+.3f}"
            )

        # Size on the price the order will actually fill at, not on the raw ask.
        # A DOWN clip buys NO, so it fills at (1 - ask). Sizing off `ask` alone
        # meant a 6c NO ask produced 5/0.06 = 83 contracts, which then filled at
        # 0.939 - a $5 clip turned into $76 of exposure. That single trade is the
        # -$73.59 that emptied the DRY account.
        fill_price = ask if side == "up" else (1.0 - ask if side else 0.0)
        contracts = self._size(fill_price, clip_usd=clip_usd) if side else 0
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
            notional=round(float(fill_price or 0.0) * contracts, 2),
            seconds_left=round(market.seconds_left or 0.0, 1),
            reason=reason,
        )
        self.book.signals = [signal]
        return signal

    def _size(self, ask: Optional[float], clip_usd: Optional[float] = None) -> int:
        """Contracts for one clip. Zero when the price cannot clear the floor.

        `clip_usd` overrides the default clip size; LIVE passes a fraction of the
        available balance so a small account keeps firing instead of emptying in
        one trade.
        """
        try:
            price = float(ask) if ask is not None else 0.0
        except (TypeError, ValueError):
            return 0
        if price <= 0 or price > 1.0:
            return 0
        target_notional = (
            self.config.notional_usd if clip_usd is None else max(clip_usd, 0.0)
        )
        if target_notional <= 0:
            return 0
        contracts = int(target_notional / price)
        if contracts * price < self.config.min_order_usd:
            contracts = int(round(self.config.min_order_usd / price, 0))
        return max(contracts, 0)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _db_path(self) -> str:
        import os

        return os.environ.get("DB_PATH", "").strip() or "trading_system.db"

    def _entry_block(
        self, signal: UpDownSignal, held: List[Dict[str, Any]]
    ) -> str:
        """Why this clip may not be taken - or "" when it may.

        Repeated clips of the same contract are allowed by design; the old guard
        refused a second clip of a market the book already held and thereby
        treated every re-entry as an error even while the contract was still
        mispriced. What replaced it is money rules, not count rules:

          - never hold both sides of one contract (the payouts sum to $1.00, so
            the pair is a certain loss of the spread)
          - the model's probability on the chosen side must clear
            `min_win_prob`, so repeated clips only accumulate while the contract
            is still more likely than not to pay
          - total open notional across all clips stays under
            `max_open_notional`
        """
        opposite = {"up": "NO", "down": "YES"}
        for p in held:
            if p["ticker"] == signal.ticker and p["side"] == opposite.get(signal.side):
                return (
                    f"already hold the opposite side of {signal.ticker}; "
                    "closing first (never both sides of one contract)"
                )

        win_prob = signal.fair if signal.side == "up" else 1.0 - signal.fair
        if win_prob < self.config.min_win_prob:
            self.book.skipped_low_prob += 1
            return (
                f"win probability {win_prob:.2f} below "
                f"{self.config.min_win_prob:.2f} on {signal.side.upper()}"
            )

        open_notional = sum(float(p.get("notional") or 0.0) for p in held)
        if open_notional + signal.notional > self.config.max_open_notional:
            return (
                f"${open_notional:.2f} already deployed; adding "
                f"${signal.notional:.2f} would exceed the "
                f"${self.config.max_open_notional:.2f} cap"
            )
        return ""

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
        #
        # `allow_duplicate=True` is what lets this strategy pyramid: buying
        # additional clips of the same contract is deliberate, gated by
        # min_win_prob and max_open_notional in `cycle`, not an accident to
        # prevent.
        try:
            position_id = await self.db_manager.add_position(position, allow_duplicate=True)
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"position insert failed: {type(exc).__name__}: {exc}"
            return False
        if position_id is None:
            self.book.last_error = f"position insert refused for {signal.ticker}"
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

        # This trader used to only ever OPEN positions. Closing them was someone
        # else's job: `run_tracking` lives inside BeastModeBot, so in DRY the
        # ai_directional process quietly closed btc_updown's positions for it.
        #
        # Arming btc_updown on its own in LIVE - exactly the plan for a small
        # first run - therefore had nothing tracking it at all: no take-profit,
        # no stop-loss, nothing but Kalshi's own settlement at the 15-minute
        # boundary, and no close ever written to trade_logs. A strategy that
        # cannot exit is not a strategy.
        #
        # So it tracks its own book now. Running tracking from more than one
        # process is safe because claim_position_for_close() admits exactly one
        # closer per position - that is what stopped the double-counted closes.
        try:
            from src.jobs.track import run_tracking

            await run_tracking(self.db_manager)
        except Exception as exc:  # noqa: BLE001 - tracking must not block entry
            self.book.last_error = f"position tracking: {type(exc).__name__}: {exc}"

        try:
            await self.feed.fetch()
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"series fetch: {type(exc).__name__}: {exc}"
            return self.book.summary()

        market = self.feed.nearest()

        # LIVE sizes the clip against the real balance, so the account keeps
        # trading on every edge instead of spending itself dark in one or two
        # clips. A read failure must not stop the cycle - it falls back to the
        # fixed clip and lets execute_position's own fail-closed balance check
        # refuse if the account genuinely cannot pay.
        live_budget: Optional[float] = None
        if live:
            try:
                if self._client is None:
                    from src.clients.kalshi_client import KalshiClient

                    self._client = KalshiClient()
                bal = await self._client.get_balance()
                cents = float((bal or {}).get("balance") or 0.0)
                live_budget = round(
                    cents / 100.0 * self.config.live_cash_fraction, 2
                )
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = f"LIVE balance read: {type(exc).__name__}: {exc}"

        signal = self.evaluate(market, live=live, clip_usd=live_budget if live else None)
        # Clear stale errors, but never wipe the explanation of a failed LIVE
        # balance read - that is the difference between "no edge" and "cannot
        # read the account".
        if not live or live_budget is not None:
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
            # A scored but un-actionable signal in LIVE is almost always a
            # zero-dollar budget: the account cannot buy the minimum clip. Say
            # that instead of reporting a silent no-op.
            if (
                live
                and signal is not None
                and not signal.actionable
                and live_budget is not None
                and live_budget <= 0.0
            ):
                refusal = (
                    "LIVE balance is $0.00 - nothing can be bought until the "
                    "account is funded"
                )

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

        # Repeated clips of the same contract are allowed - deliberately. The old
        # guard refused a second clip of a market the book already held, which
        # treated every re-entry as an error even when the contract was still
        # mispriced. What it actually produced was one $5 bet on a 15-minute
        # window, decided once, at whatever noise the quote happened to carry.
        #
        # The replacement rules are money rules, not count rules:
        #   - never hold both sides of one contract (certain loss of the spread)
        #   - only enter when the model's probability on this side clears
        #     min_win_prob, so repeated clips only accumulate while the contract
        #     is still genuinely more likely than not to pay
        #   - total open notional across ALL clips stays under max_open_notional
        if signal is not None and signal.actionable:
            blocked = self._entry_block(signal, held)
            if not blocked and await self._place(signal, live):
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
                "live_budget": live_budget,
                "required_edge": round(
                    self.config.min_edge + (self.config.live_fee_rate if live else 0.0), 4
                ),
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
