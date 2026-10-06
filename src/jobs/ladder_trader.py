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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.config.settings import settings
from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket

# A Kalshi quote older than this is not traded against: the book simply has
# not repriced yet, so any "edge" versus fresh truth is fiction.
QUOTE_MAX_AGE_SEC = 30.0
# LIVE-ONLY: max open clips of one ticker. One 15-minute bucket is one bet;
# DRY is uncapped so its historic pyramiding pattern is untouched.
LIVE_MAX_CLIPS_PER_TICKER = 3
# NOTE: a LIVE-only 0.10-0.50 entry band lived here and was removed: it cut
# LIVE to 52 skips and 0 trades an hour. Both books now share the sweet-band
# surcharge in UpDownConfig; LIVE adds only the real fee.


@dataclass
class UpDownConfig:
    """Hard limits. Sized so a single bad call cannot damage the account."""

    notional_usd: float = 5.0
    # Required disagreement with Kalshi's own price before trading.
    min_edge: float = 0.06
    # STANDARD TUNING, learned from the forever trade log (283 closes):
    #   - entries at $0.90 and up win 4% of the time ($-110.82): hard-blocked
    #   - the $0.25-$0.50 band wins 100% ($363.34): entries outside it need
    #     EXTRA edge before they are worth taking
    #   - NO trades win 83% vs YES at 67%: the NO side is preferred unless YES
    #     is clearly better
    max_entry_price: float = 0.60  # REDUCED: 11% win above 0.65, stop the bleeding
    sweet_band_low: float = 0.20
    sweet_band_high: float = 0.50
    out_of_band_extra_edge: float = 0.04  # RAISED: Now out-of-band is 0.50-0.60, needs +4c edge
    prefer_side: str = "down"
    up_override_margin: float = 0.02
    # Fee per fill. Kalshi charges 0.07 * price * (1-price) per
    # contract on every fill, so a 6c edge that clears DRY is only
    # a ~3c edge LIVE once the fee is taken out. Both books now
    # pay this fee (dry_fee_enabled) so the simulated P&L matches
    # reality. The actual fee is computed from the fill price at
    # decision time, not this placeholder.
    live_fee_rate: float = 0.03
    # LIVE only: the largest fraction of the available balance one clip may
    # spend. A fixed $5 clip empties a small account in a trade or two and it
    # then sits idle all night - sizing down against the balance keeps the book
    # firing on every real edge instead of going dark after one buy. Raised to
    # 0.20 on the strength of the forever-log analysis (76% win rate over 283
    # closes - the edge is real, scale up).
    live_cash_fraction: float = 0.20
    # LIVE only: skip the historically losing 18 UTC hour entirely. The
    # forever log went 17 trades at 41.2% for -$2.70 there while every
    # neighbouring hour printed; demanding extra edge still paid the fee on
    # a coin flip, so LIVE sits the hour out. DRY never reads this flag and
    # still trades the hour.
    live_skip_losing_hour: bool = True
    # AI advisory stack. All default OFF: with every flag off the ladder is
    # byte-identical math, and the ai_* modules are never even imported on
    # the trading path. Env overrides let Railway enable one lane at a time
    # without a code change (AI_VETO_ENABLED=1, SENTINEL_ENABLED=1).
    ai_veto_enabled: bool = False
    sentinel_enabled: bool = False
    sentinel_state_path: str = "sentinel_state.json"
    # DRY fakes the same fee LIVE pays so the simulated P&L is
    # realistic. Without this DRY looks profitable simply because
    # it never paid Kalshi's taker fee. Controlled by DRY_FAKE_FEES
    # env var (default "1" = enabled).
    dry_fee_enabled: bool = False
    # Extra edge demanded while the sentinel reads caution (event risk in
    # the air but unconfirmed). Halt blocks outright; see _ai_guards.
    sentinel_caution_extra_edge: float = 0.03

    def __post_init__(self) -> None:
        import os as _os

        if _os.environ.get("AI_VETO_ENABLED", "") == "1":
            self.ai_veto_enabled = True
        if _os.environ.get("SENTINEL_ENABLED", "") == "1":
            self.sentinel_enabled = True
        _sp = _os.environ.get("SENTINEL_STATE_PATH", "")
        if _sp:
            self.sentinel_state_path = _sp
        # DRY fakes the same fee LIVE pays so the simulated P&L is
        # realistic. Without this DRY looks profitable simply because
        # it never paid Kalshi's taker fee.
        if _os.environ.get("DRY_FAKE_FEES", "1") == "1":
            self.dry_fee_enabled = True

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
    # Which truth the score used: kalshi-brti (windowed/avg60/value) or
    # coinbase-spot. Added for the BRTI feed; defaults keep old rows valid.
    truth: str = ""

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
    skipped_session: int = 0
    skipped_ai_veto: int = 0
    skipped_sentinel: int = 0
    last_error: str = ""
    dry: bool = True

    def summary(self) -> Dict[str, Any]:
        return {
            "signals": len(self.signals),
            "actionable": sum(1 for s in self.signals if s.actionable),
            "open_positions": len(self.open_positions),
            "open_notional": round(
                sum(float(p.get("notional") or 0.0) for p in self.open_positions), 2
            ),
            "max_open_notional": self.max_open_notional,
            "trades_today": self.trades_today,
            "skipped_no_edge": self.skipped_no_edge,
            "skipped_stale": self.skipped_stale,
            "skipped_too_close": self.skipped_too_close,
            "skipped_unquoted": self.skipped_unquoted,
            "skipped_low_prob": self.skipped_low_prob,
            "skipped_session": self.skipped_session,
            "skipped_ai_veto": self.skipped_ai_veto,
            "skipped_sentinel": self.skipped_sentinel,
            "last_error": self.last_error,
            "dry": self.dry,
        }


def fair_up_probability(spot: float, target: float, noise_usd: float = 15.0, seconds_left: float = 900.0) -> float:
    """P(the next window settles at or above the target), from live spot.

    A logistic in the distance past the target, in units of the noise band.
    The noise band is scaled by remaining time: with less time left, the
    same spot-vs-target distance is more decisive; with more time, more
    uncertainty. At the target it is exactly 0.5, which is the right prior
    for a market that has not moved yet; far past it, it saturates near 1.
    """
    if noise_usd <= 0:
        return 0.5
    time_scale = math.sqrt(max(seconds_left, 1.0) / 900.0)
    effective_noise = noise_usd * time_scale
    z = max(-6.0, min(6.0, (spot - target) / effective_noise))
    return float(1.0 / (1.0 + math.exp(-z)))


class UpDownTrader:
    """Scores the next 15-minute contract against live spot and takes one clip."""

    def __init__(
        self,
        spot: SpotFeed,
        feed: Btc15mFeed,
        config: Optional[UpDownConfig] = None,
        db_manager: Any = None,
        brti: Any = None,
    ):
        self.spot = spot
        self.feed = feed
        self.config = config or UpDownConfig()
        self.db_manager = db_manager
        # BRTI truth feed (Kalshi's own CF Benchmarks index). None means
        # Coinbase spot only -- every existing caller keeps working.
        self.brti = brti
        self._client: Any = None
        # Lazy OpenRouter client for the veto judge. Built only when
        # ai_veto_enabled, so DRY never pays for it.
        self._ai_client: Any = None
        self.book = UpDownBook(max_open_notional=self.config.max_open_notional)

    def evaluate(
        self,
        market: Optional[UpDownMarket],
        live: bool = False,
        clip_usd: Optional[float] = None,
    ) -> Optional[UpDownSignal]:
        """Score the next contract. Read-only: no orders here.

        `live` raises the edge bar by Kalshi's actual taker fee
        (0.07 * price * (1-price)). DRY also fakes this fee
        (dry_fee_enabled) so the simulated P&L matches what LIVE
        actually pays. An edge that clears DRY but not the fee is
        a losing trade with real money. `clip_usd` overrides the
        fixed clip size (used in LIVE to size down against the
        available balance).
        """
        # LIVE-ONLY losing-hour skip: the 18 UTC hour lost money in the
        # forever log, so LIVE sits it out. A losing hour is cheaper to skip
        # than to re-learn with real money. DRY never enters this branch.
        if live and self.config.live_skip_losing_hour:
            try:
                from src.jobs import live_fees as _live_fees_hr

                if _live_fees_hr.live_session_skip():
                    self.book.skipped_session += 1
                    return None
            except Exception:  # noqa: BLE001 - a clock error never buys an entry
                pass
        if market is None:
            self.book.skipped_unquoted += 1
            return None
        if not market.tradable or market.target is None:
            self.book.skipped_unquoted += 1
            return None
        # A stale Kalshi quote plus a fresh truth is a fake edge: the book
        # simply hasn't repriced yet. Refuse when the quote itself is old.
        # Feeds that never fetched (ts == 0, the unit-test shape) skip this:
        # in production cycle() always fetches before scoring.
        try:
            _feed_ts = float(getattr(self.feed, "ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            _feed_ts = 0.0
        if _feed_ts > 0:
            try:
                _quote_age = max(0.0, time.time() - _feed_ts)
            except (TypeError, ValueError):
                _quote_age = float("inf")
            if not getattr(self.feed, "markets", None) or _quote_age > QUOTE_MAX_AGE_SEC:
                self.book.skipped_stale += 1
                return None
        # Truth selection: both books use real Coinbase spot only.
        # DRY no longer falls back to BRTI — the same leading
        # indicator LIVE trades against. This makes DRY a genuine
        # rehearsal: it scores the same signal and pays the same
        # fees, so its P&L is a realistic preview of LIVE.
        truth_price = 0.0
        truth_kind = ""
        if self.spot.price > 0 and self.spot.age <= self.config.max_spot_age:
            truth_price = self.spot.price
            truth_kind = f"coinbase-{self.spot.source or 'spot'}"
        if truth_price <= 0:
            self.book.skipped_stale += 1
            return None
        spot = truth_price
        # No entries inside the no-entry window (both books): the settlement
        # window is the final 60 seconds, and a fill in there is a coin toss
        # whether the truth is spot or the windowed average.
        if (market.seconds_left or 0) < self.config.min_seconds_left:
            # The settlement window is the final 60 seconds. Inside it,
            # spot is no longer leading anything.
            self.book.skipped_too_close += 1
            return None

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
                    f"{truth_kind} {spot:,.0f} is {delta:+,.0f} from target {target:,.0f} - "
                    f"inside the ${self.config.noise_usd:,.0f} noise band, no trade"
                ),
                truth=truth_kind,
            )
            self.book.signals = [signal]
            return signal

        fair = fair_up_probability(spot, target, self.config.noise_usd, market.seconds_left or 900.0)

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

        # ONE decision rule for both books: DRY's min_edge. LIVE adds
        # exactly what costs real money - Kalshi's actual taker fee
        # (0.07 * price * (1-price)) - and nothing else. No hour
        # bans, no LIVE-only bands, no refusal DRY does not also have:
        # if DRY would take the trade, LIVE takes it. The edge that
        # built DRY's ledger is the edge LIVE trades on now.
        # DRY also fakes the fee (dry_fee_enabled) so the simulated
        # P&L matches what LIVE actually pays.
        def _required(fill_price: Optional[float]) -> float:
            if not live and not self.config.dry_fee_enabled:
                return self.config.min_edge
            kalshi_fee = 0.07 * (1.0 - float(fill_price or 0.5))
            return self.config.min_edge + kalshi_fee

        up_fill = up_ask if up_ask is not None else None
        down_fill = down_ask if down_ask is not None else None

        def _side_ok(edge: float, fill_price: Optional[float], req: float) -> bool:
            if fill_price is None or fill_price <= 0.0:
                return False
            # Hard block from the log: $0.90+ entries won 4% of the time.
            if fill_price >= self.config.max_entry_price:
                return False
            r = req
            # The sweet band wins 100%; outside it, demand more edge.
            if not (self.config.sweet_band_low <= fill_price <= self.config.sweet_band_high):
                r += self.config.out_of_band_extra_edge
            return edge >= r

        up_ok = _side_ok(up_edge, up_fill, _required(up_fill))
        down_ok = _side_ok(down_edge, down_fill, _required(down_fill))

        side = ""
        ask: Optional[float] = None
        kalshi: Optional[float] = None
        edge = 0.0
        if self.config.prefer_side == "down":
            # NO wins 83% vs YES at 67%: prefer NO unless YES is clearly better.
            if down_ok and (not up_ok or down_edge >= up_edge - self.config.up_override_margin):
                side, ask, kalshi, edge = "down", down_ask, down_ask, down_edge
            elif up_ok:
                side, ask, kalshi, edge = "up", up_ask, up_ask, up_edge
        else:
            if up_ok and up_edge >= down_edge:
                side, ask, kalshi, edge = "up", up_ask, up_ask, up_edge
            elif down_ok:
                side, ask, kalshi, edge = "down", down_ask, down_ask, down_edge

        if not side:
            self.book.skipped_no_edge += 1
            _req = (self.config.min_edge + 0.07 * (1.0 - float(up_fill or 0.5))) if live else self.config.min_edge
            reason = (
                f"{truth_kind} {spot:,.0f} vs target {target:,.0f} ({delta:+,.0f}): "
                f"fair {fair:.2f}, Kalshi up "
                f"{('%.2f' % up_ask) if up_ask is not None else '--'} / down "
                f"{('%.2f' % down_ask) if down_ask is not None else '--'} - "
                f"best edge {max(up_edge, down_edge):+.3f} under {_req:.3f}"
                 + (" (fee-aware)" if (live or self.config.dry_fee_enabled) else "")
            )
            if (up_fill is not None and up_fill >= self.config.max_entry_price) or (
                down_fill is not None and down_fill >= self.config.max_entry_price
            ):
                reason += " - entry price in the blocked $0.90+ band"
        else:
            reason = (
                f"{truth_kind} {spot:,.0f} vs target {target:,.0f} ({delta:+,.0f}): "
                f"fair {('%.2f' % (fair if side == 'up' else 1.0 - fair))} vs Kalshi "
                f"{float(kalshi or 0.0):.2f} on {side.upper()} - edge {edge:+.3f}"
            )

        # Size on the price the order will actually fill at: the side's own ask.
        # UP fills at yes_ask, DOWN fills at no_ask. An earlier revision filled
        # DOWN at (1 - ask) -- the UP price for a DOWN bet -- which booked $48
        # of exposure as a $4.93 clip and credited exits at the real price: ~$43
        # of phantom profit per trade. That inversion is what flattered the DRY
        # NO line. Both sides fill at their own ask now.
        fill_price = ask if side else 0.0
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
            truth=truth_kind,
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
        target_notional = self.config.notional_usd if clip_usd is None else max(clip_usd, 0.0)
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
        self, signal: UpDownSignal, held: List[Dict[str, Any]], live: bool = False
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

        LIVE-ONLY: at most LIVE_MAX_CLIPS_PER_TICKER clips of one ticker. One
        15-minute bucket is one coin flip -- the 26-clip pyramid of 2026-10-04
        put $21.50 of real exposure on a single flip. DRY keeps unlimited
        pyramiding so its historic pattern is untouched.
        """
        opposite = {"up": "NO", "down": "YES"}
        for p in held:
            if p["ticker"] == signal.ticker and p["side"] == opposite.get(signal.side):
                return (
                    f"already hold the opposite side of {signal.ticker}; "
                    "closing first (never both sides of one contract)"
                )
        if live and sum(1 for p in held if p["ticker"] == signal.ticker) >= (
            LIVE_MAX_CLIPS_PER_TICKER
        ):
            return (
                f"already hold {LIVE_MAX_CLIPS_PER_TICKER} LIVE clips of"
                f" {signal.ticker}; one bucket is one bet, no more pyramiding"
            )

        win_prob = signal.fair if signal.side == "up" else 1.0 - signal.fair
        if win_prob < self.config.min_win_prob:
            self.book.skipped_low_prob += 1
            return (
                f"win probability {win_prob:.2f} below "
                f"{self.config.min_win_prob:.2f} on {signal.side.upper()}"
            )

        open_notional = sum(float(p.get("notional") or 0.0) for p in held)
        if live and open_notional + signal.notional > self.config.max_open_notional:
            return (
                f"${open_notional:.2f} already deployed; adding "
                f"${signal.notional:.2f} would exceed the "
                f"${self.config.max_open_notional:.2f} cap"
            )
        return ""

    async def _ai_guards(self, signal: UpDownSignal, live: bool) -> str:
        """Sentinel flag + AI veto, both fail-open. Returns '' or the block.

        With both flags off (the default) this is one branch and no imports:
        the ladder is byte-identical math. Enabled lanes degrade to math on
        any error -- an advisor that cannot be read is permission, never a
        halt (except an explicit sentinel halt, which is the point of it).
        """
        if self.config.sentinel_enabled:
            try:
                from src.jobs import sentinel as _sentinel

                _state, _reason = _sentinel.read_flag(self.config.sentinel_state_path)
                if _state == "halt":
                    self.book.skipped_sentinel += 1
                    return f"sentinel halt: {_reason}"[:200]
                if _state == "caution":
                    _kalshi_fee = 0.07 * (1.0 - float(signal.ask or 0.5)) if (live or self.config.dry_fee_enabled) else 0.0
                    _r = (
                        self.config.min_edge
                        + _kalshi_fee
                        + self.config.sentinel_caution_extra_edge
                    )
                    _fill = float(signal.ask or 0.0)
                    if not (self.config.sweet_band_low <= _fill <= self.config.sweet_band_high):
                        _r += self.config.out_of_band_extra_edge
                    if float(signal.edge or 0.0) < _r:
                        self.book.skipped_sentinel += 1
                        return (f"sentinel caution demands {_r:.2f} edge" f" ({_reason})")[:200]
            except Exception:  # noqa: BLE001 - sentinel errors never block
                pass
        if self.config.ai_veto_enabled:
            try:
                from src.jobs import ai_veto as _ai_veto
                from src.jobs import venue_feeds as _venue

                if self._ai_client is None:
                    from src.clients.openrouter_client import OpenRouterClient

                    # DRY uses the free OpenRouter key/model so it
                    # doesn't burn the paid balance. LIVE uses the
                    # real key from settings.
                    _api_key = (
                        settings.api.dry_openrouter_api_key
                        if not live
                        else None
                    )
                    _model = (
                        settings.api.dry_openrouter_model
                        if not live
                        else None
                    )
                    self._ai_client = OpenRouterClient(
                        api_key=_api_key,
                        default_model=_model,
                    )
                _streak = "unknown"
                try:
                    _recent = await self._recent_side_outcomes(signal.side)
                    _streak = _recent
                except Exception:  # noqa: BLE001
                    _streak = "unknown"
                _venue_state = _venue.snapshot()
                _clip = {
                    "ticker": signal.ticker,
                    "side": signal.side,
                    "ask": signal.ask,
                    "edge": signal.edge,
                    "fair": signal.fair,
                    "seconds_left": signal.seconds_left,
                    "streak": _streak,
                    "vol_pct": 0.0,
                    "headlines": "none",
                    "required": 0.07 * (1.0 - float(signal.ask or 0.5))
                    if (live or self.config.dry_fee_enabled)
                    else self.config.min_edge,
                }
                _veto = await _ai_veto.check_veto(self._ai_client, _clip, _venue_state)
                if _veto:
                    self.book.skipped_ai_veto += 1
                    return f"AI veto: {_veto}"[:200]
            except Exception:  # noqa: BLE001 - veto errors never block
                pass
        return ""

    async def _recent_side_outcomes(self, side: str, limit: int = 6) -> str:
        """Same-side streak context for the veto prompt, from trade_logs."""
        if self.db_manager is None:
            return "unknown"
        try:
            import aiosqlite

            db_path = getattr(self.db_manager, "db_path", None) or self._db_path()
            async with aiosqlite.connect(db_path) as conn:
                cur = await conn.execute(
                    "SELECT side, pnl FROM trade_logs WHERE strategy='btc_updown'"
                    " ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                )
                rows = await cur.fetchall()
        except Exception:  # noqa: BLE001
            return "unknown"
        losses = 0
        for _s, _pnl in rows:
            if str(_s or "").lower() != str(side or "").lower():
                break
            try:
                if float(_pnl or 0.0) <= 0:
                    losses += 1
                else:
                    break
            except Exception:  # noqa: BLE001
                break
        if losses == 0:
            return "no streak"
        return f"{losses} same-side losses in a row"

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
        # Both sides fill at their own ask (UP at yes_ask, DOWN at no_ask).
        # See the note at sizing: (1 - ask) here once booked $48 as $4.93.
        price = ask
        # LIVE-ONLY maker entry (item 2): with time on the clock, rest the buy
        # at the side's bid (maker fee ~1/4 of taker) and wait a few seconds
        # for the book to come to us. The signal prices the edge at the ask,
        # so a fill at the bid is strictly better than the price that already
        # cleared the bar. No usable bid -> the taker path below, unchanged.
        _maker_wait = 0.0
        if live:
            try:
                from src.jobs import live_fees as _live_fees_mk
                from src.utils.market_prices import get_market_prices as _gmp

                if _live_fees_mk.should_use_maker_entry(signal.seconds_left):
                    _md = await self._client.get_market(signal.ticker)
                    _yb, _ya, _nb, _na = _gmp((_md or {}).get("market") or {})
                    _bid = _yb if signal.side == "up" else _nb
                    _mk = _live_fees_mk.maker_entry_price(signal.side, _bid, ask)
                    if _mk is not None:
                        price = _mk
                        _maker_wait = _live_fees_mk.MAKER_ENTRY_WAIT_SECONDS
            except Exception:  # noqa: BLE001 - a failed bid read falls back to taker
                _maker_wait = 0.0
                price = ask
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
            filled = await execute_position(
                position,
                live,
                self.db_manager,
                self._client,
                maker_wait_seconds=_maker_wait,
            )
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
            # LIVE-ONLY fetch throttle: far from expiry the book barely moves,
            # so reuse a fresh cache instead of hammering /markets into 429s.
            # DRY always fetches (aggressive rehearsal); LIVE reuses a cache
            # younger than 15s when more than 180s remain.
            _use_cache = False
            if live:
                try:
                    import time as _time

                    _age = _time.time() - float(getattr(self.feed, "ts", 0.0) or 0.0)
                    _nearest_cached = self.feed.nearest()
                    _left = (
                        float(_nearest_cached.seconds_left or 0.0)
                        if _nearest_cached is not None
                        else 0.0
                    )
                    if _nearest_cached is not None and _age < 15.0 and _left > 180.0:
                        _use_cache = True
                except Exception:  # noqa: BLE001 - fall through to fetch
                    _use_cache = False
            if not _use_cache:
                await self.feed.fetch()
        except Exception as exc:  # noqa: BLE001
            # LIVE-ONLY 429 resilience: on rate-limit, reuse the last good
            # cache with jittered backoff instead of blanking the cycle.
            # DRY keeps the old fail-fast behaviour untouched.
            _msg = f"{type(exc).__name__}: {exc}"
            _is_429 = "429" in _msg or "Too Many Requests" in _msg
            if live and _is_429 and getattr(self.feed, "markets", None):
                try:
                    import asyncio as _asyncio

                    from src.jobs import live_fees as _live_fees

                    await _asyncio.sleep(_live_fees.backoff_delay_seconds(1))
                except Exception:  # noqa: BLE001
                    pass
                self.book.last_error = f"series fetch 429 (LIVE cache reused): {_msg[:120]}"
            else:
                self.book.last_error = f"series fetch: {_msg}"
                return self.book.summary()

        market = self.feed.nearest()

        # LIVE sizes the clip against the real balance, so the account keeps
        # trading on every edge instead of spending itself dark in one or two
        # clips. A read failure must not stop the cycle - it falls back to the
        # fixed clip and lets execute_position's own fail-closed balance check
        # refuse if the account genuinely cannot pay.
        live_budget: Optional[float] = None
        variance_mult: float = 1.0
        if live:
            try:
                if self._client is None:
                    from src.clients.kalshi_client import KalshiClient

                    self._client = KalshiClient()
                bal = await self._client.get_balance()
                cents = float((bal or {}).get("balance") or 0.0)
                live_budget = round(cents / 100.0 * self.config.live_cash_fraction, 2)
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = f"LIVE balance read: {type(exc).__name__}: {exc}"
            # LIVE-ONLY variance-commensurate sizing: the bigger Kalshi's lie
            # (spot far from target with minutes to close), the bigger the clip
            # -- up to 2.5x -- because convergence is proportionally more
            # certain. Bounded by the balance fraction, the $1 minimum order
            # floor and max_open_notional downstream. DRY keeps its fixed $5.
            if live_budget is not None and market is not None:
                try:
                    from src.jobs import live_fees as _live_fees_var

                    _tgt = market.target
                    # Variance is measured on the same truth the score uses:
                    # BRTI estimate when fresh, else retail spot.
                    _ref = 0.0
                    try:
                        _b = getattr(self, "brti", None)
                        if _b is not None and bool(getattr(_b, "fresh", False)):
                            _ref = float(_b.estimate() or 0.0)
                    except Exception:  # noqa: BLE001
                        _ref = 0.0
                    if _ref <= 0:
                        _ref = self.spot.price
                    if _tgt is not None and _ref > 0:
                        variance_mult = _live_fees_var.variance_clip_multiplier(
                            _ref - float(_tgt),
                            self.config.noise_usd,
                        )
                        live_budget = round(live_budget * variance_mult, 2)
                except Exception:  # noqa: BLE001 - sizing never blocks entry
                    variance_mult = 1.0

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
                    "LIVE balance is $0.00 - nothing can be bought until the " "account is funded"
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
            blocked = self._entry_block(signal, held, live=live)
            if not blocked:
                blocked = await self._ai_guards(signal, live)
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
        # LIVE-ONLY session bump is reported truthfully; DRY reports the raw bar.
        _session_bump = 0.0
        if live:
            try:
                from src.jobs import live_fees as _live_fees_rep

                _session_bump = float(_live_fees_rep.live_session_extra_edge())
            except Exception:  # noqa: BLE001
                _session_bump = 0.0
        # BRTI observability: why the score used retail spot instead of the
        # index (degraded reason, staleness) -- read from the cycle print.
        try:
            _b = getattr(self, "brti", None)
            if _b is None:
                _brti_state = "no-feed"
            elif bool(getattr(_b, "fresh", False)):
                _brti_state = f"live-{_b.estimate_kind()}:{_b.estimate():,.0f}"
            else:
                _brti_state = (
                    f"stale-{getattr(_b, 'degraded_reason', '') or 'no ticks'}"
                    f"[frames={getattr(_b, 'frames_seen', 0)}"
                    f" last={getattr(_b, 'last_msg_type', '') or '-'}]"
                    f"{(';' + str(getattr(_b, 'last_error', ''))[:80]) if getattr(_b, 'last_error', '') else ''}"
                )
        except Exception:  # noqa: BLE001
            _brti_state = "unknown"
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
                "variance_mult": variance_mult if live else 1.0,
                "truth_source": (signal.truth if signal else "") or "none",
                "brti_state": _brti_state,
                "required_edge": round(
                    self.config.min_edge
                    + (0.07 * (1.0 - float(signal.ask or 0.5))
                       if (live or self.config.dry_fee_enabled) else 0.0)
                    + _session_bump,
                    4,
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
    """Run the 15-minute up/down trader, optionally on a loop until interrupted.

    THE PROCESS IS IMMORTAL. This used to let any exception in `cycle()` - or
    in `hub.start()` before the try block - escape, exit the process, and wait
    for the supervisor to notice and respawn it. Between the crash and the
    respawn the card read "stopped", and under a crash loop it read "stopped"
    more often than not. That is what "it never stays on" was. Nothing except
    SIGTERM/SIGKILL (the operator's Stop, or a container kill) ends this loop
    now: every pass is wrapped, failures are printed and retried with backoff.
    """
    from src.jobs.market_data import MarketDataHub

    hub = MarketDataHub()
    # BRTI truth: Kalshi's own settlement index over its own socket. Starts
    # degraded without keys and the trader falls back to Coinbase; never fatal.
    # DRY no longer uses BRTI — both books use Coinbase spot only.
    brti = None
    try:
        from src.jobs.brti_feed import BrtiFeed

        brti = BrtiFeed()
    except Exception as exc:  # noqa: BLE001
        print(f"BTC 15m: BRTI feed unavailable ({exc}); coinbase-spot only", flush=True)
        brti = None
    trader = UpDownTrader(hub.spot, hub.feed, config, brti=brti)
    sleep_for = interval or (config and config.poll_seconds) or 4.0
    started = False
    backoff = 1.0
    # LIVE-only; created on the first due pass so DRY never builds one.
    reaper = None
    try:
        while True:
            try:
                if not started:
                    try:
                        await hub.start()
                        if brti is not None:
                            try:
                                await brti.start()
                            except Exception as exc2:  # noqa: BLE001 - degraded ok
                                print(
                                    f"BTC 15m: BRTI start failed "
                                    f"({type(exc2).__name__}); coinbase-spot only",
                                    flush=True,
                                )
                    except Exception as exc:  # noqa: BLE001 - start may be retried
                        print(
                            f"BTC 15m: market data hub failed to start "
                            f"({type(exc).__name__}: {exc}); retrying in {backoff:.0f}s",
                            flush=True,
                        )
                    else:
                        started = True

                result = await trader.cycle()
                print(result, flush=True)
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                print(
                    f"BTC 15m pass failed ({type(exc).__name__}: {exc}); "
                    f"retrying in {backoff:.0f}s",
                    flush=True,
                )
                try:
                    await hub.stop()
                except Exception:  # noqa: BLE001
                    pass
                started = False
                backoff = min(backoff * 2, 30.0)

            # Constant live proof: stamp that this process executed a pass just
            # now. The dashboard reads this stamp and refuses to call the
            # strategy "running" once it goes stale - a recycled pid cannot
            # fake it, because only the real process can write it. Scoped to
            # this process's own book: a DRY heartbeat never counts as a LIVE
            # proof of life or the other way around.
            try:
                from src.jobs.broker import should_trade_live
                from src.utils.strategy_runtime import StrategyRuntime

                await StrategyRuntime(db_path=trader._db_path()).record_heartbeat(
                    "btc_updown", "live" if should_trade_live() else "paper"
                )
            except Exception as exc:  # noqa: BLE001 - proof must not kill the loop
                print(f"BTC 15m: heartbeat write failed: {type(exc).__name__}: {exc}", flush=True)

            # LIVE-ONLY book reaper: every 15 minutes, kill local rows Kalshi
            # holds nothing behind (freeing the strategy slot they occupied) and
            # liquidate Kalshi holdings no local row is tracking. Real money
            # moves here, so it only ever runs on the LIVE client.
            try:
                from src.jobs.broker import should_trade_live

                if should_trade_live():
                    from src.jobs.live_reaper import LiveReaper

                    if reaper is None:
                        reaper = LiveReaper(trader.db_manager, trader._client)
                    summary = await reaper.maybe_run()
                    if summary:
                        if summary.get("failed"):
                            print(
                                f"LIVE reaper FAILED: {summary['error']}",
                                flush=True,
                            )
                        else:
                            print(
                                f"LIVE reaper: {summary['phantom_closed']} phantom rows "
                                f"closed, {len(summary['liquidated'])} orphan holdings "
                                f"liquidated, {len(summary['shorts_flagged'])} shorts "
                                f"flagged, {summary.get('holdings_after')} Kalshi "
                                f"holdings left ({summary['flat_rows_on_kalshi']} flat "
                                f"rows ignored)",
                                flush=True,
                            )
            except Exception as exc:  # noqa: BLE001 - reaping never kills the loop
                print(f"BTC 15m: reaper pass failed: {type(exc).__name__}: {exc}", flush=True)

            if not loop:
                return
            await asyncio.sleep(max(sleep_for, backoff))
    finally:
        try:
            await hub.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if brti is not None:
                await brti.stop()
        except Exception:  # noqa: BLE001
            pass
