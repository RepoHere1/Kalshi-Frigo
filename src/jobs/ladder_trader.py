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
# MAX CLIPS OF ONE TICKER: ONE, both books. Evidence from the live trade log
# (2026-10-06, 145 closed trades): every dollar-large loss (-$8.16, -$6.27,
# -$4.87, -$4.31) was a PYRAMID - two or three clips stacked on the SAME
# 15-minute flip. Each clip passed the money rules alone, but they are 100%
# correlated: the aggregate bet was 3-4x the Kelly cap on a single flip, and
# when the flip lost, the whole stack died at once. The kelly_cap is a limit
# PER INDEPENDENT BET; re-betting the same edge is not a new bet. One window,
# one coin flip, one Kelly-sized clip.
MAX_CLIPS_PER_TICKER = 1
# NOTE: a LIVE-only 0.10-0.50 entry band lived here and was removed: it cut
# LIVE to 52 skips and 0 trades an hour. Both books now share the sweet-band
# surcharge in UpDownConfig; LIVE adds only the real fee.

# Per-asset tuning. BTC's class defaults were derived from BTC's market
# microstructure: a ~$84,000 asset whose "sweet band" sits at 0.20-0.50 and
# whose 15-minute sigma is ~$17 (0.02% of price). Sub-dollar and mid-cap
# assets move far more, relative to price, and their market makers quote the
# near-certain side at 0.64-0.88, so BTC's deadband and entry caps refuse the
# very trades those lanes exist to take. Keyed on the Coinbase spot product.
# Values are deliberately conservative: noise widened to a fraction of each
# asset's own realized move, and entry caps lifted only to admit a real edge,
# never to reopen the $0.90+ "4%-win" band (that hard block stays global).
ASSET_TUNING: Dict[str, Dict[str, float]] = {
    "XRP-USD": {
        "noise_pct": 0.0008,          # ~0.08% of price: XRP/XAU move like BTC/ETH
        "max_entry_price": 0.60,
        "max_entry_price_maker": 0.75,
        "sweet_band_low": 0.20,
        "sweet_band_high": 0.50,
    },
    "XAU-USD": {
        "noise_pct": 0.0003,          # ~0.03% of price (~$0.90): Gold moves steadily
        "max_entry_price": 0.85,      # Gold markets quote 0.64-0.88 (verified live)
        "max_entry_price_maker": 0.85,
        # Sweet band widened: gold markets live in 0.60-0.80 (near-certain
        # quote is dominant because gold's volatility is much smaller than
        # BTC's); the 0.20-0.50 band refuses every real trade.
        "sweet_band_low": 0.55,
        "sweet_band_high": 0.80,
    },
    "ETH-USD": {
        "noise_pct": 0.0002,          # ~0.02% of price (~$0.51): ETH/XAU are large
                                      # asset like BTC; 0.1% was far too wide
        "max_entry_price": 0.60,
        "max_entry_price_maker": 0.75,
        "sweet_band_low": 0.20,
        "sweet_band_high": 0.50,
    },
    "SOL-USD": {
        "noise_pct": 0.0006,          # ~0.06% of price: tightened, still above BTC
        "max_entry_price": 0.65,
        "max_entry_price_maker": 0.75,
        "sweet_band_low": 0.20,
        "sweet_band_high": 0.65,
    },
}


@dataclass
class UpDownConfig:
    """Hard limits. Sized so a single bad call cannot damage the account."""

    notional_usd: float = 5.0
    # Required disagreement with Kalshi's own price before trading.
    min_edge: float = 0.06  # canonical spec value (was 0.045)
    # STANDARD TUNING, learned from the forever trade log (283 closes):
    #   - entries at $0.90 and up win 4% of the time ($-110.82): hard-blocked
    #   - the $0.25-$0.50 band wins 100% ($363.34): entries outside it need
    #     EXTRA edge before they are worth taking
    #   - NO trades win 83% vs YES at 67%: the NO side is preferred unless YES
    #     is clearly better
    max_entry_price: float = 0.55  # Tightened: only trade $0.40-$0.55 sweet spot
    min_entry_price: float = 0.40  # NEW: block entries below 40¢
    # LIVE log showed entries at 0.80-0.85 YES resolve to zero almost every time
    # (the $0.90+ "4% win" band bleeding lower). Cap maker entries below that.
    yes_extra_edge: float = 0.03   # YES/UP must beat its bar by +0.03 (NO wins more)
    sweet_band_low: float = 0.20
    sweet_band_high: float = 0.50
    out_of_band_extra_edge: float = 0.04  # RAISED: Now out-of-band is 0.50-0.60, needs +4c edge
    prefer_side: str = "up"
    up_override_margin: float = 0.02
    # Fee per fill. Kalshi charges 0.07 * price * (1-price) per
    # contract on every fill, so a 6c edge that clears DRY is only
    # a ~3c edge LIVE once the fee is taken out. Both books now
    # pay this fee (dry_fee_enabled) so the simulated P&L matches
    # reality. The actual fee is computed from the fill price at
    # decision time, not this placeholder.
    live_fee_rate: float = 0.03
    # The largest fraction of the available balance one clip may
    # spend. A fixed $5 clip empties a small account in a trade or two and it
    # then sits idle all night - sizing down against the balance keeps the book
    # firing on every real edge instead of going dark after one buy. Raised to
    # 0.20 on the strength of the forever-log analysis (76% win rate over 283
    # closes - the edge is real, scale up).
    live_cash_fraction: float = 0.20
    # FRACTIONAL KELLY SIZING - the compounding engine. The mathematically
    # growth-optimal fraction of the book for a binary priced c with model
    # probability f is k* = (f - c) / (1 - c); betting kelly_scale * k* gives
    # most of the growth for a fraction of the drawdown. kelly_cap bounds a
    # single clip no matter how wide the edge claims to be. This replaces the
    # fixed live_cash_fraction as the primary LIVE (and, by the law, DRY)
    # sizer: clips grow as the book grows and shrink into drawdowns, which is
    # the only way $10 compounds into something instead of flatlining.
    kelly_sizing: bool = True
    kelly_scale: float = 0.25
    kelly_cap: float = 0.35    # never more than 35% of the book in one clip
    kelly_confidence_pivot: float = 0.70  # fair probability at which kelly_scale begins scaling up
    kelly_confidence_max_mult: float = 2.0  # max multiplier on kelly_scale (capped at 0.50)
    # High-probability convergence entries: a resting maker bid at $0.60-$0.85
    # pays a quarter fee and, with the diffusion fair value demanding real
    # edge over it, is the steadiest compounding trade this book has. Taker
    # entries stay capped at max_entry_price - the taker fee at 85c is 6% of
    # the clip, poison at this scale. The $0.90+ hard block stands: entries
    # there won 4% forever, fee schedule irrelevant.
    max_entry_price_maker: float = 0.75
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
    # THE LAW in reverse: DRY must rehearse what LIVE actually does, and
    # LIVE's cheap edge is the maker entry - resting the buy at the side's
    # bid for MAKER_ENTRY_WAIT_SECONDS instead of crossing the spread. With
    # this on, DRY rests the same virtual limit against the same real order
    # book and only books a fill when the book actually crosses it, so the
    # simulated P&L carries the maker fee advantage (and the fill risk).
    # DRY_MAKER_ENTRY=0 reverts DRY to always-taker.
    dry_maker_entry: bool = True
    # One-venue dislocation guard (the operator's observation: Coinbase can
    # drop $50 while the venues Kalshi settles on drop $20-$40). When the
    # other major USD venues sit on the OPPOSITE side of the target by more
    # than this budget, the move is one venue's order flow - not the market's
    # - and the entry is refused. Kraken/Bitstamp, keyless, degrade to no-op.
    venue_guard_enabled: bool = True
    venue_guard_pct: float = 0.0007
    # IMPLIED vs MEASURED volatility (the hedged-vol trade's core read).
    # Kalshi's price inverted through the diffusion is the market's implied
    # sigma; RealizedVol measures the tape. When implied is RICH vs realized
    # the binary overcharges for movement and entries demand extra edge;
    # when implied is CHEAP the entry is already getting paid for vol.
    # The Hyperliquid perp hedge that flattens direction is planned on every
    # entry and marked against the keyless public BTC mid; ORDERING on HL
    # stays off until the operator sets HL_HEDGE_ENABLED=1 with creds.
    vol_edge_enabled: bool = True
    vol_rich_ratio: float = 1.25
    vol_rich_extra_edge: float = 0.02
    vol_cheap_ratio: float = 0.80
    vol_cheap_edge_credit: float = 0.01
    # POLYMARKET guard/arb: Polymarket runs the same 15-minute BTC window on
    # the same clock (verified). When the two venues disagree beyond this
    # threshold on the same event, entries are refused; when the combined
    # cost of both sides across venues undercuts $1 by more than the fee
    # budget, the arb is logged on the signal (Poly leg = logged intent
    # until a CLOB executor exists).
    poly_guard_enabled: bool = True
    poly_guard_threshold: float = 0.10
    # MOMENTUM FILTER: Poly drift confirmation before entry.
    # When Polymarket's 15-min window drifts opposite our signal direction,
    # reject the entry as the move may reverse before our window closes.
    momentum_filter_enabled: bool = True
    momentum_agree_threshold: float = 0.03  # Poly drift must be within ±0.03 of our direction
    # MAKER-ONLY ORDERS: Post limit orders only, never cross spread for liquidity
    maker_only_mode: bool = True  # NEW: Only place limit orders at bid/ask
    maker_wait_seconds: float = 5.0  # Wait time for maker order to fill

    # QUICK_WIN: safe high-confidence mode for tiny accounts ($7).
    # Only takes very certain trades (high edge, sweet spot only), very small clips,
    # quick exit. No unlimited leverage — just faster capture of confirmed edges.
    quick_win_enabled: bool = False  # DEAD: experiment removed 2026-10-09
    quick_win_edge: float = 0.15     # (kept only for the entry clamp default)
    quick_win_max_clip_usd: float = 6.00  # Scaled to $7 account: take $6 wins
    quick_win_profit_target: float = 0.08  # 8% quick profit take
    quick_win_stop_loss: float = 0.03      # 3% tight stop
    quick_win_sweet_low: float = 0.30
    quick_win_sweet_high: float = 0.45

    # CERTAIN-WIN PAIR (the operator's "take larger amounts on a provable
    # win" rule). YES + NO together pay exactly $1.00 at settlement; when both
    # ASKS plus both taker fees leave a net, buying both sides is arithmetic,
    # not a prediction. This is the one place size may be larger than an edge
    # clip - bounded by cash and the notional law, never unlimited.
    certain_win_enabled: bool = True
    certain_win_min_net: float = 0.015   # $ net per pair, after both fees
    certain_win_max_usd: float = 50.0    # per-pair budget cap
    certain_win_cash_fraction: float = 0.5

    # FRESH-BURST (latency capture, new): when the last ~20s of spot moved
    # >=2.5 sigma of that lookback and the contract quote has not repriced,
    # the entry goes TAKER instead of resting at the bid - the one moment
    # the taker fee buys something. Real moves with runway only; the veto's
    # explosion/chase rules still apply on top.
    burst_enabled: bool = True
    burst_lookback_sec: float = 20.0
    burst_sigma_mult: float = 2.5
    burst_min_seconds_left: float = 180.0

    # CORRELATED-CRYPTO CAP (new): BTC/ETH/XRP/HYPE move together - four
    # same-direction lanes are ONE bet wearing four hats. Cap the total
    # same-side up/down exposure so a single reversal cannot hit every
    # lane at once.
    correlated_cap_usd: float = 15.0

    # === INVENTION #3: Order-Flow + Liquidity Sentinel ===
    # Veto entries when the book is too wide or shows adverse imbalance.
    # Both books share this gate so DRY rehearses LIVE flow conditions.
    flow_sentinel_enabled: bool = True

    # === INVENTION #4: Drawdown-Aware Dynamic Kelly ===
    # Fraction scales down linearly from peak_floor=0.18 to floor_usd=20.
    # Same rule for both books; LIVE adds the real balance, DRY uses its own.
    drawdown_kelly_enabled: bool = True
    drawdown_peak_floor: float = 0.18

    # === INVENTION #11: Volatility-Targeted Sizing ===
    # Scale notional to fixed target_vol (0.75% portfolio vol by default).
    # Only shrinks when realized vol is HOTTER than target.
    vol_target_enabled: bool = True
    vol_target_pct: float = 0.0075

    # === INVENTION #10: Live Shadow Backtester Filter ===
    # Skip entries when historical analogs show <min_win_rate and <min_payout.
    # Pass through with not-enough-data; refuse when enough data disagrees.
    shadow_backtest_enabled: bool = True
    shadow_min_win_rate: float = 0.57
    shadow_min_payout: float = 2.1

    # === INVENTION #7: Dedicated Macro Veto Agent ===
    # A parallel macro-only LLM that can VETO on macro risk.
    # Disabled by default (no headline source); enable with MACRO_VETO_ENABLED=1.
    macro_veto_enabled: bool = False

    # TIERED ENTRY BAR (new): the flat min_edge treated a 57% view and a
    # 90% view as equal. Strong views (>= strong_prob) get a LOWER bar so
    # the lanes fire more of the trades the model actually believes - more
    # trades, and a higher win rate per trade, at zero new refusals. The
    # marginal cushion (marginal_edge_scale) ships at 1.0 - the record's
    # "0.55-0.65 loses" claim needs its own evidence pass before it is
    # allowed to refuse anything the fee alone would not.
    tiered_edge_enabled: bool = True
    strong_prob: float = 0.72
    strong_edge_scale: float = 0.5  # genius: more high-conviction entries (2x rate target)
    marginal_prob: float = 0.65
    marginal_edge_scale: float = 1.0

    # CONVICTION MULTIPLIER (new): the tiered bar finds the high-confidence
    # trades; this sizes them up to `conviction_mult` x on top of Kelly.
    # Bounded by every existing money cap - and by its own fraction ceiling.
    conviction_sizing: bool = True
    conviction_mult: float = 2.0
    conviction_max_fraction: float = 0.5  # hard ceiling: balance share/clip
    full_conviction_mult: float = 3.0  # multiplier when 3+ signals agree
    volume_spike_threshold: float = 1000.0  # volume above which counts as a spike

    # CERTIFIED CERTAINTY (new): a view at/above certified_prob - the model
    # AND the price agreeing with a big cushion - sizes at certified_mult.
    # The only "bet more on near certainty" that is not degen: certification
    # is an information edge (fair must still clear price + bar), never a
    # feeling, and every existing ceiling (fraction, budget, notional,
    # correlation) still binds the result.
    certified_prob: float = 0.85
    certified_mult: float = 3.0

    # US-EQUITY SESSION BIAS: scale clip size to liquidity. Higher Kalshi
    # volume during the US session means tighter spreads = more edge/trade.
    session_mult_enabled: bool = True
    session_mult_high: float = 1.30   # 9:30-11:30 AM ET (highest volume)
    session_mult_normal: float = 1.00  # 11:30 AM-4:00 PM ET
    session_mult_low: float = 0.70     # 4:00 PM-9:30 AM ET (next day)

    def __post_init__(self) -> None:
        import os as _os

        if _os.environ.get("AI_VETO_ENABLED", "") == "1":
            self.ai_veto_enabled = True
        if _os.environ.get("SENTINEL_ENABLED", "") == "1":
            self.sentinel_enabled = True
        # Convert legacy env vars to the new percentage-based fields
        if "NOISE_USD" in _os.environ:
            old_noise = float(_os.environ["NOISE_USD"])
            self.noise_pct = old_noise / 100.0  # backward compat
        if "VENUE_GUARD_MAX_USD" in _os.environ:
            old_guard = float(_os.environ["VENUE_GUARD_MAX_USD"])
            self.venue_guard_pct = old_guard / 100.0
        _sp = _os.environ.get("SENTINEL_STATE_PATH", "")
        if _sp:
            self.sentinel_state_path = _sp
        # DRY fakes the same fee LIVE pays so the simulated P&L is
        # realistic. Without this DRY looks profitable simply because
        # it never paid Kalshi's taker fee.
        if _os.environ.get("DRY_FAKE_FEES", "1") == "1":
            self.dry_fee_enabled = True
        if _os.environ.get("DRY_MAKER_ENTRY", "1") != "1":
            self.dry_maker_entry = False
        if _os.environ.get("VENUE_GUARD", "1") != "1":
            self.venue_guard_enabled = False
        if _os.environ.get("CERTAIN_WIN", "1") != "1":
            self.certain_win_enabled = False
        if _os.environ.get("CERTAIN_WIN_MIN_NET"):
            try:
                self.certain_win_min_net = float(_os.environ["CERTAIN_WIN_MIN_NET"])
            except ValueError:
                pass
        if _os.environ.get("POLY_GUARD", "1") != "1":
            self.poly_guard_enabled = False
        if _os.environ.get("VOL_EDGE", "1") != "1":
            self.vol_edge_enabled = False
        if _os.environ.get("KELLY_SIZING", "1") != "1":
            self.kelly_sizing = False
        # (The QUICK_WIN experiment is removed for good: it forced a 15%
        # minimum edge no real 15-minute market clears, which - stacked with
        # the venue-guard scoping bug - is why every lane sat silent. The
        # fields above remain as inert defaults for the entry clamp only.)
        try:
            self.kelly_scale = float(_os.environ.get("KELLY_SCALE", self.kelly_scale))
            self.kelly_cap = float(_os.environ.get("KELLY_CAP", self.kelly_cap))
            self.kelly_confidence_pivot = float(
                _os.environ.get("KELLY_CONFIDENCE_PIVOT", self.kelly_confidence_pivot)
            )
            self.kelly_confidence_max_mult = float(
                _os.environ.get("KELLY_CONFIDENCE_MAX_MULT", self.kelly_confidence_max_mult)
            )
            self.max_entry_price_maker = float(
                _os.environ.get("MAX_ENTRY_MAKER", self.max_entry_price_maker)
            )
        except ValueError:
            pass
        _vg = _os.environ.get("VENUE_GUARD_PCT", "")
        if _vg:
            try:
                self.venue_guard_pct = float(_vg)
            except ValueError:
                pass

    # Minimum model probability on the chosen side before any entry. The old
    # "one clip per market" guard was removed; this is what replaces it as the
    # anti-churn rule - repeated buys of the same contract are allowed, but only
    # while the contract is still more likely than not to pay.
    min_win_prob: float = 0.55
    # (removed the edge-override escape: a sub-0.55 win probability cannot be
    # guaranteed a profitable pre-settlement exit on a 15-min bucket, so it is
    # refused outright rather than bought on the mispricing theory. Lowered
    # 0.60 -> 0.55 on 2026-10-09: 0.60 + the fee-aware edge gate stacked into
    # never trading on near-the-money dislocations the market genuinely
    # mispriced; 0.55 keeps the majority-odds rule while letting those fire)
    # SURVIVAL / HYPER-CAUTION: when the real balance is at or below
    # `survival_floor_usd`, the book is one losing clip from being unable to
    # trade at all (no outside funding will ever arrive). So it raises the bar
    # hard: a much higher edge, a much higher win probability, and a smaller
    # clip, so the next few trades are overwhelmingly likely to win and grow
    # the balance back to a buffer that can absorb a loss. This is temporary
    # survivability, not the steady-state tuning.
    survival_enabled: bool = True
    survival_floor_usd: float = 20.0
    survival_min_edge: float = 0.25        # only near-certain disagreements
    survival_min_win_prob: float = 0.90    # 90%+ model probability
    survival_clip_fraction: float = 0.10   # bet a tenth of the book, max
    # Total simulated/live notional the book may hold open across ALL clips,
    # including multiple clips of the same contract. This replaces the old
    # max_open_positions position COUNT, which forbade a second clip of the same
    # market even when the odds justified it.
    max_open_notional: float = 25.0
    # Deadband around the target, as a fraction of the target price.
    # Settlement is a 60-second average of a composite index, so sub-noise
    # moves are not information. The effective dollar noise scales with
    # the asset price: BTC at $84,000 gets ~$17 noise, XRP at $0.50 gets ~$0.0004.
    noise_pct: float = 0.0002
    # Kalshi's minimum order is one contract. There is no dollar-notional
    # floor: a 1-cent contract is a valid $0.01 order. The old $1.00 minimum
    # made it impossible for a tiny account to ever place an order.
    min_order_usd: float = 0.0
    # Stop opening new positions this close to expiry: the settlement window is
    # the 60 seconds before close, and a fill inside it is a coin toss.
    min_seconds_left: float = 45.0
    poll_seconds: float = 4.0
    max_spot_age: float = 5.0

    def apply_asset(self, spot_product: Optional[str]) -> None:
        """Apply per-asset deadband and entry-band overrides.

        Called once by the runner with the lane's Coinbase pair so a sub-dollar
        asset (XRP) or Gold (XAU) is not graded against BTC's microstructure
        defaults. Unknown products keep BTC defaults unchanged.
        """
        _prod = (spot_product or "").strip().upper()
        # Gold proxy: Coinbase has no XAU-USD product, so the gold lane prices
        # against PAXG-USD (1 troy oz of tokenised gold). Grade it with the
        # XAU tuning rather than BTC's microstructure defaults.
        if _prod == "PAXG-USD":
            _prod = "XAU-USD"
        tuning = ASSET_TUNING.get(_prod)
        if not tuning:
            return
        self.noise_pct = tuning["noise_pct"]
        self.max_entry_price = tuning["max_entry_price"]
        self.max_entry_price_maker = tuning.get("max_entry_price_maker", self.max_entry_price_maker)
        self.sweet_band_low = tuning.get("sweet_band_low", self.sweet_band_low)
        self.sweet_band_high = tuning.get("sweet_band_high", self.sweet_band_high)


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
    skipped_venue_guard: int = 0
    skipped_poly_divergence: int = 0
    skipped_momentum: int = 0
    skipped_correlated: int = 0
    skipped_flow_veto: int = 0
    skipped_macro_veto: int = 0
    skipped_shadow: int = 0
    skipped_drawdown: int = 0
    skipped_quick_win: int = 0
    skipped_news_sentiment: int = 0
    last_error: str = ""
    blocked: str = ""
    dry: bool = True
    # CERTAIN-WIN pair counters (the arithmetic-only trades).
    certain_wins: int = 0
    certain_win_net: float = 0.0

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
            "skipped_venue_guard": self.skipped_venue_guard,
            "skipped_poly_divergence": self.skipped_poly_divergence,
            "skipped_momentum": self.skipped_momentum,
            "skipped_correlated": self.skipped_correlated,
            "skipped_news_sentiment": self.skipped_news_sentiment,
            "last_error": self.last_error,
            "dry": self.dry,
            "certain_wins": self.certain_wins,
            "certain_win_net": round(self.certain_win_net, 4),
        }


def _money(value: Any) -> str:
    """Format a price/delta with decimals that suit its magnitude.

    Sub-$1 assets need sub-cent precision; large assets want thousands separators.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    a = abs(v)
    if a == 0:
        return "0"
    if a < 1:
        return f"{v:.6f}".rstrip("0").rstrip(".")
    if a < 100:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if a < 10_000:
        return f"{v:,.2f}"
    return f"{v:,.0f}"


def _min_viable_notional(cents: float) -> float:
    """The smallest dollar clip a balance of `cents` can still submit.

    One contract at a 1-cent price is $0.01, so even a few cents of balance can
    trade. Returns the floor we will not size a clip below so a tiny account
    keeps firing a real (whole-contract) order rather than zeroing out.
    """
    # At minimum one contract at the worst-case ~99c price; in practice clips
    # are bounded downstream by the actual fill price. This is just a sane
    # dollar floor so a 2-cent balance still submits something concrete.
    return max(0.01, round(cents / 100.0, 4))


def _get_et_hour() -> int:
    """Current hour in US Eastern Time (EST/EDT), 0-23."""
    from datetime import datetime, timezone, timedelta

    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    offset = -4 if 3 <= month <= 11 else -5
    et_now = utc_now + timedelta(hours=offset)
    return et_now.hour


def session_multiplier(cfg: Any) -> float:
    """US equity market hours multiplier for clip sizing.

    9:30-11:30 AM ET:  highest Kalshi volume, scale up 30%.
    11:30 AM-4:00 PM ET: normal hours, no change.
    4:00 PM-9:30 AM ET: reduced volume, scale down 30%.
    """
    if not getattr(cfg, "session_mult_enabled", True):
        return 1.0
    et_hour = _get_et_hour()
    if 9 <= et_hour < 11:
        return float(getattr(cfg, "session_mult_high", 1.30))
    elif 11 <= et_hour < 16:
        return float(getattr(cfg, "session_mult_normal", 1.00))
    else:
        return float(getattr(cfg, "session_mult_low", 0.70))


def tiered_edge_bar(win_prob: float, base_edge: float, cfg: Any) -> float:
    """The raw edge bar before fees, scaled by the model's own confidence.

    One flat bar treats a 57% view and a 90% view as equal; they are not.
    STRONG views (>= strong_prob) are the trades the model actually has an
    opinion on - their bar drops so the lane fires MORE of them (more
    trades, and a higher win rate per trade). MARGINAL views (below
    marginal_prob) must pay a bigger cushion, because the record shows the
    0.55-0.65 zone is where losing entries cluster. Fees are added after
    this scaling, unchanged.
    """
    base = float(base_edge)
    if getattr(cfg, "tiered_edge_enabled", True):
        if float(win_prob) >= cfg.strong_prob:
            base *= cfg.strong_edge_scale
        elif float(win_prob) < cfg.marginal_prob:
            base *= cfg.marginal_edge_scale
    return max(base, 0.0)


def conviction_scale(
    clip_usd: Optional[float], win_prob: float, cfg: Any
) -> Optional[float]:
    """Clip size multiplied by conviction - the money maximizer.

    The strong-view discount (tiered bar) finds the trades the model
    believes most; this makes them PAY: a view at/above strong_prob sizes up
    to `conviction_mult` x on top of whatever Kelly produced. The money
    laws downstream (correlated cap, max_open_notional, cash fraction)
    still bound the result - aggression never escapes a cap.
    """
    if not getattr(cfg, "conviction_sizing", True):
        return clip_usd
    if float(win_prob) < cfg.strong_prob:
        return clip_usd
    base = float(clip_usd) if clip_usd is not None else float(cfg.notional_usd)
    mult = float(cfg.conviction_mult)
    certified = getattr(cfg, "certified_prob", None)
    if certified is not None and float(win_prob) >= float(certified):
        mult = max(mult, float(getattr(cfg, "certified_mult", mult)))
    return round(base * mult, 2)


def fair_up_probability(
    spot: float,
    target: float,
    noise: float = 15.0,
    seconds_left: float = 900.0,
    sigma_dollars: Optional[float] = None,
) -> float:
    """P(the next window settles at or above the target), from live spot.

    `noise` is the deadband in dollars (target * noise_pct).
    The effective dollar noise scales with the target.

    - WITH a realized-vol estimate (`sigma_dollars` from RealizedVol): the
      honest diffusion probability `Phi((spot - target) / (sigma * sqrt(t)))`.
      This is what a binary on a driftless walk is worth, and it is why the
      operator's "spot moved $50 but the contract moved $30" is geometry, not
      fraud - the move that matters is measured in sigmas of the time left.
    - WITHOUT one (fresh process, sparse prints): the legacy logistic in the
      distance past the target, in units of the noise band, scaled by
      remaining time. Kept as the cold-start fallback so a fresh process
      behaves exactly like the old book until the vol window fills.

    At the target both are exactly 0.5; far past it, both saturate.
    """
    time_scale = math.sqrt(max(seconds_left, 1.0) / 900.0)
    if sigma_dollars is not None and sigma_dollars > 0:
        effective = float(sigma_dollars) * time_scale
        z = max(-6.0, min(6.0, (spot - target) / effective))
        return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
    if noise <= 0:
        return 0.5
    effective_noise = noise * time_scale
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
        lane: str = "btc_updown",
    ):
        self.spot = spot
        self.feed = feed
        self.config = config or UpDownConfig()
        self.db_manager = db_manager
        # This process's own lane identity. Each crypto (BTC/XRP/XAU/ETH) is a
        # SEPARATE book and must attribute its positions and veto streaks to its
        # own lane, never to a shared "btc_updown" bucket - otherwise the four
        # lanes fight over one position table and one guard.
        self.lane = lane
        # BRTI truth feed (Kalshi's own CF Benchmarks index). None means
        # Coinbase spot only -- every existing caller keeps working.
        self.brti = brti
        self._client: Any = None
        # Lazy OpenRouter client for the veto judge. Built only when
        # ai_veto_enabled, so DRY never pays for it.
        self._ai_client: Any = None
        # Realized volatility from the same spot prints the strategy trades
        # against: the honest denominator of the fair-value S-curve.
        from src.jobs.realized_vol import RealizedVol

        self.rv = RealizedVol()
        # One-venue dislocation guard (Kraken/Bitstamp consensus vs Coinbase).
        from src.jobs.venue_dislocation import VenueDislocation

        self.venue_guard = VenueDislocation(
            max_dislocation_usd=0.0  # set dynamically per-market
        )
        # SCOPE THE GUARD TO THE BTC LANE. The Kraken/Bitstamp feeds inside
        # VenueDislocation are hardcoded BTC pairs; on any other underlying
        # the "dislocation" compared BTC's price (~81,707) against, say,
        # XRP's 1.38 and vetoed every single entry forever ("gap +81,707").
        # A dislocation read only exists for the asset those feeds carry.
        if getattr(feed, "series", "KXBTC15M") != "KXBTC15M":
            self.config.venue_guard_enabled = False
        # Polymarket same-window scanner + Hyperliquid mark (both keyless).
        from src.jobs.cross_venue_poly import PolyScanner
        from src.jobs.vol_edge import HyperliquidMark

        self.poly = PolyScanner()
        self.hl = HyperliquidMark()
        self.poly_arb_last: Optional[Dict[str, Any]] = None
        self._vol_adj: float = 0.0
        # FRESH-BURST flag for this pass: a fresh spot move the quote has
        # not repriced. Set by _score; consumed by the maker/taker choice.
        self._burst: bool = False
        self._poly_window: Optional[Any] = None  # reused poly window for signal stacking
        # Certain-win pair bookkeeping: cooldown + max rounds per ticker, so
        # the arithmetic trade repeats a few times per market at most.
        self._cw_last: Dict[str, float] = {}
        self._cw_rounds: Dict[str, int] = {}
        self._sigma_used: Optional[float] = None
        self._impl_sigma: Optional[float] = None
        # Per-pass book balances, for the Kelly sizer. DRY caches its own
        # simulated cash so both books size by the SAME rule on their OWN
        # money - the law.
        self._live_balance: Optional[float] = None
        self._dry_cash_cache: Optional[float] = None
        # INVENTION #7: macro veto cache - one verdict per ticker per process.
        self._macro_cache: Dict[str, Any] = {}
        # INVENTION #12: risk overlay state for drawdown and vol target.
        self._peak_balance: Optional[float] = None
        self._realized_vol: Optional[float] = None
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
        # The old 18-UTC "losing hour" skip was removed: a time-of-day block is an
        # arbitrary impediment - the trade the model sees now is what matters, and
        # sitting out a whole hour both books was a guaranteed way to miss edge.
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
        # Every print feeds the realized-vol window; the fair value below
        # switches from the fixed-band logistic to the measured diffusion
        # once enough of the tape has been seen.
        self.rv.observe(spot)
        # No entries inside the no-entry window (both books): the settlement
        # window is the final 60 seconds, and a fill in there is a coin toss
        # whether the truth is spot or the windowed average.
        if (market.seconds_left or 0) < self.config.min_seconds_left:
            # The settlement window is the final 60 seconds. Inside it,
            # spot is no longer leading anything.
            self.book.skipped_too_close += 1
            return None

        target = float(market.target)
        # Full float precision. A fixed round(..., 2) flattened sub-dollar
        # assets (XRP at $0.50) to exactly 0,
        # so every pass read "spot == target" and landed in skipped_no_edge
        # forever. Keep all digits; format only for display.
        delta = spot - target
        # === INVENTION #3: Order-Flow + Liquidity Sentinel ===
        # Veto entries with wide spreads or adverse flow imbalance.
        # Applied identically to BOTH books - DRY rehearses LIVE flow conditions.
        # Skipped if bids are zero (incomplete book or test fixture).
        # Skipped if bids > 25 cents from ask (unrealistic / inverted book).
        if self.config.flow_sentinel_enabled and market.up_price is not None and market.down_price is not None:
            try:
                from src.jobs.flow_sentinel import (
                    compute_flow_metrics,
                    flow_sentinel,
                )
                _fm = compute_flow_metrics(
                    yes_ask=float(market.up_price or 0.0),
                    yes_bid=float(getattr(market, "yes_bid", 0) or 0.0),
                    no_ask=float(market.down_price or 0.0),
                    no_bid=float(getattr(market, "no_bid", 0) or 0.0),
                    yes_depth=int(getattr(market, "yes_depth", 0) or 0),
                    no_depth=int(getattr(market, "no_depth", 0) or 0),
                )
                _pass, _why = flow_sentinel(_fm)
                if not _pass:
                    self.book.skipped_flow_veto += 1
                    return UpDownSignal(
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
                        reason=f"flow sentinel: {_why}",
                        truth=truth_kind,
                    )
            except Exception:  # noqa: BLE001 - flow sentinel never blocks
                pass
        # === INVENTION #7: Dedicated Macro Veto Agent ===
        # A parallel macro-only LLM call that can VETO on macro risk.
        # Fail-open: with no headlines, default score=0 (no veto).
        if getattr(self.config, "macro_veto_enabled", False):
            try:
                from src.jobs.macro_veto import macro_verdict

                _mv = macro_verdict(
                    market_title=market.ticker,
                    event_category="crypto-15min",
                    cache=self._macro_cache,
                )
                if _mv.get("vetoes"):
                    self.book.skipped_macro_veto += 1
                    return UpDownSignal(
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
                        reason=f"macro veto: {_mv.get('reason', 'risk')}",
                        truth=truth_kind,
                    )
            except Exception:  # noqa: BLE001 - macro veto fails open
                pass
        # One-venue dislocation guard: Kalshi settles on the multi-venue BRTI
        # composite, not on Coinbase. If the other major venues sit on the
        # OPPOSITE side of the target by more than the budget, this "lead"
        # is one venue's order flow and the entry is refused. Degrades to
        # no-op when the extra feeds are unreachable.
        if self.config.venue_guard_enabled:
            try:
                # venue_guard_pct scales with target price
                self.venue_guard.max_dislocation_usd = target * self.config.venue_guard_pct
                _veto, _vreason = self.venue_guard.vetoes(
                    "up" if delta > 0 else "down", delta, spot
                )
                if _veto:
                    self.book.skipped_venue_guard += 1
                    return UpDownSignal(
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
                        reason=f"venue guard: {_vreason} - not the market's move",
                        truth=truth_kind,
                    )
            except Exception:  # noqa: BLE001 - the guard never blocks on its own failure
                pass
        # Polymarket runs the SAME 15-minute window on the same clock. A hard
        # disagreement on the identical event is information - refuse the
        # entry rather than argue with a second real-money venue.
        if self.config.poly_guard_enabled and market.bucket is not None:
            try:
                from src.jobs.cross_venue_poly import bucket_to_utc

                _end = bucket_to_utc(market.bucket)
                _pdiv = (
                    self.poly.diverges_from(
                        _end, market.up_price, self.config.poly_guard_threshold
                    )
                    if _end is not None
                    else None
                )
                if _pdiv:
                    self.book.skipped_poly_divergence += 1
                    return UpDownSignal(
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
                        reason=f"poly divergence: {_pdiv} - same event, other venue disagrees",
                        truth=truth_kind,
                    )
            except Exception:  # noqa: BLE001 - guard failure never blocks
                pass
        # MOMENTUM FILTER: reject counter-momentum entries.
        # If Polymarket's 15-min window is drifting opposite our signal,
        # the move may reverse before our window closes.
        if self.config.momentum_filter_enabled and market.bucket is not None:
            try:
                from src.jobs.cross_venue_poly import bucket_to_utc

                _end = bucket_to_utc(market.bucket)
                if _end is not None:
                    _w = self.poly.window_for(_end)
                    self._poly_window = _w  # cache for multi-signal conviction stacking
                    if _w is not None:
                        _poly_drift = _w.up - _w.down
                        _our_drift = delta
                        if (_our_drift > 0 and _poly_drift < -self.config.momentum_agree_threshold) or \
                           (_our_drift < 0 and _poly_drift > self.config.momentum_agree_threshold):
                            self.book.skipped_momentum += 1
                            return UpDownSignal(
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
                                reason=f"momentum filter: poly drift {_poly_drift:+.2f} vs our drift {_our_drift:+.2f}",
                                truth=truth_kind,
                            )
            except Exception:  # noqa: BLE001 - momentum filter never blocks on its own failure
                pass
        # Hard deadband.
        # noise scales with target price so XRP/XAU/ETH/BTC all work.
        noise_usd = target * self.config.noise_pct
        if abs(delta) <= noise_usd:
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
                    f"{truth_kind} {_money(spot)} is {_money(delta)} from target "
                    f"{_money(target)} - "
                    f"inside the ${_money(noise_usd)} noise band, no trade"
                ),
                truth=truth_kind,
            )
            self.book.signals = [signal]
            return signal

        seconds_left = market.seconds_left or 900.0
        # Realized $-vol for exactly the time that is left. None until the
        # window has enough tape; the legacy logistic covers the cold start.
        self._sigma_used = self.rv.sigma_dollars(
            horizon_seconds=seconds_left, last_price=spot
        )
        # IMPLIED sigma: Kalshi's own price inverted through the diffusion.
        # A RICH implied vs realized is usually momentum - the diffusion fair
        # value already prices the move, so penalizing it again would
        # double-count and block every trending window. Rich is therefore
        # ANNOTATED only; cheap implied keeps its small credit, because the
        # entry genuinely gets paid to carry volatility the market says won't
        # happen.
        self._impl_sigma: Optional[float] = None
        self._vol_adj = 0.0
        if self.config.vol_edge_enabled and self._sigma_used:
            try:
                from src.jobs.vol_edge import implied_sigma

                if market.up_price is None or target is None:
                    self._impl_sigma = None
                else:
                    self._impl_sigma = implied_sigma(
                        market.up_price, spot, target, seconds_left
                    )
                    if self._impl_sigma is not None and self._impl_sigma < (
                        self._sigma_used * self.config.vol_cheap_ratio
                    ):
                        self._vol_adj = -self.config.vol_cheap_edge_credit
            except Exception:  # noqa: BLE001 - vol read never blocks scoring
                self._impl_sigma = None
        # FRESH-BURST (latency capture, new): a quick oversized move in the
        # last ~20s that the contract quote has not repriced is the one
        # moment worth PAYING the taker fee instead of resting at the bid -
        # the edge decays in seconds, not minutes. Real (>=2.5 sigma of the
        # lookback) and with runway (>=180s) only; the veto's explosion and
        # chase rules still apply on top.
        self._burst = False
        if self.config.burst_enabled and self._sigma_used:
            try:
                _rm = self.rv.recent_move(self.config.burst_lookback_sec)
                if _rm is not None and seconds_left >= self.config.burst_min_seconds_left:
                    _b_delta, _b_span = _rm
                    _b_sigma = self._sigma_used * (
                        max(float(_b_span), 1.0) / max(float(seconds_left), 1.0)
                    ) ** 0.5
                    if _b_sigma > 0 and abs(float(_b_delta)) >= (
                        self.config.burst_sigma_mult * _b_sigma
                    ):
                        self._burst = True
            except Exception:  # noqa: BLE001 - a burst upgrade never blocks
                self._burst = False
        fair = fair_up_probability(
            spot, target, target * self.config.noise_pct, seconds_left, self._sigma_used
        )

        up_ask = market.up_price
        down_ask = market.down_price

        # MAKER-FIRST PRICING. A resting order fills at the BID and pays a
        # QUARTER of the taker fee (live_fees.maker_fee_dollars models
        # Kalshi's schedule). When the clock allows a rest AND the book is
        # tight, the edge is priced at the bid and the bar drops to the maker
        # fee - charging full taker on a resting entry was invented resistance
        # that a tiny account cannot afford. A wide or crossed book falls back
        # to taker math: nobody sells into a 47c-wide bid, and an edge priced
        # against such a bid is fiction.
        try:
            from src.jobs.live_fees import should_use_maker_entry

            _maker_clock = bool(should_use_maker_entry(seconds_left))
        except Exception:  # noqa: BLE001
            _maker_clock = False
        _up_ref: float = float(up_ask) if up_ask is not None else 0.0
        _down_ref: float = float(down_ask) if down_ask is not None else 0.0
        _up_maker = False
        _down_maker = False
        if _maker_clock:
            try:
                _ub = float(market.yes_bid or 0.0)
                _ya = float(market.yes_ask or 0.0)
                _nb = float(market.no_bid or 0.0)
                _na = float(market.no_ask or 0.0)
            except (TypeError, ValueError):
                _ub = _ya = _nb = _na = 0.0
            _up_spread = (_ya - _ub) if (_ub > 0 and _ya > 0) else None
            _down_spread = (_na - _nb) if (_nb > 0 and _na > 0) else None
            if _up_spread is not None and 0.0 < _up_spread <= 0.05:
                _up_maker = True
                _up_ref = _ub
            if _down_spread is not None and 0.0 < _down_spread <= 0.05:
                _down_maker = True
                _down_ref = _nb

        if self._burst:
            # Take the quote NOW: a resting bid will not fill before the move
            # is priced in. The taker fee is the cost of capturing the burst.
            _up_maker = False
            _down_maker = False

        # Symmetric comparison: what this contract is worth to us, minus what
        # Kalshi charges for it. Both sides are a probability in [0, 1], so the
        # edge is directly comparable.
        #
        # The DOWN side is `1 - fair_up`, not `fair_up - down_ask`: if spot sits
        # $300 below the target then DOWN is nearly certain, so the edge on DOWN
        # is what we are actually being offered. Getting that backwards made the
        # strategy refuse precisely the trades it exists to take.
        up_edge = (fair - _up_ref) if up_ask is not None else 0.0
        down_edge = ((1.0 - fair) - _down_ref) if down_ask is not None else 0.0

        # ONE decision rule for both books: DRY's min_edge. LIVE adds
        # exactly what costs real money - Kalshi's actual taker fee
        # (0.07 * price * (1-price)) - and nothing else. No hour
        # bans, no LIVE-only bands, no refusal DRY does not also have:
        # if DRY would take the trade, LIVE takes it. The edge that
        # built DRY's ledger is the edge LIVE trades on now.
        # DRY also fakes the fee (dry_fee_enabled) so the simulated
        # P&L matches what LIVE actually pays.
        def _required(
            fill_price: Optional[float],
            maker: bool = False,
            win_prob: Optional[float] = None,
        ) -> float:
            # Same fee math for BOTH books so DRIED edge bar matches LIVE exactly.
            _fee_rate = 0.07 * (1.0 - float(fill_price or 0.5))
            kalshi_fee = 0.25 * _fee_rate if maker else _fee_rate
            if win_prob is None:
                _base = float(self.config.min_edge)
            else:
                # TIERED BAR: the model's confidence prices the cushion.
                _base = tiered_edge_bar(win_prob, self.config.min_edge, self.config)
            return max(_base + kalshi_fee + self._vol_adj, 0.0)

        # The band check and sizing run on the price the entry actually pays:
        # the maker bid when resting, else the taker ask.
        up_fill = _up_ref if up_ask is not None else None
        down_fill = _down_ref if down_ask is not None else None

        def _side_ok(
            edge: float, fill_price: Optional[float], req: float, maker: bool = False
        ) -> bool:
            if fill_price is None or fill_price <= 0.0:
                return False
            # Hard block from the log, both schedules: $0.90+ entries won 4%
            # of the time. No fee schedule changes that.
            if fill_price >= 0.90:
                return False
            # Makers may reach 0.85 (quarter fee, real edge required over the
            # resting bid); takers stop at the classic 0.60 - the taker fee up
            # there is a private tax on a tiny account.
            _cap = (
                self.config.max_entry_price_maker if maker else self.config.max_entry_price
            )
            if fill_price > _cap:
                return False
            r = req
            # The sweet band wins 100%; outside it, demand more edge.
            if not (self.config.sweet_band_low <= fill_price <= self.config.sweet_band_high):
                r += self.config.out_of_band_extra_edge
            return edge >= r

        # Each side's required bar is priced by ITS OWN win probability:
        # the model's confidence is what decides how much cushion is needed.
        _up_wp = float(fair)
        _down_wp = 1.0 - float(fair)
        up_ok = _side_ok(up_edge, up_fill, _required(up_fill, _up_maker, _up_wp), _up_maker)
        down_ok = _side_ok(
            down_edge, down_fill, _required(down_fill, _down_maker, _down_wp), _down_maker
        )

        # LIVE trade-log truth: YES loses net (40 losses vs 27 for NO) while NO
        # wins more (83% vs 67% forever-log). YES must therefore clear a HIGHER
        # edge bar than NO before it is worth taking - the asymmetry is real
        # money, not a tie-breaker. This is the single highest-leverage lever
        # on the win rate: stop the marginal YES entries that bleed.
        if self.config.yes_extra_edge > 0 and up_ok:
            up_ok = up_edge >= (
                _required(up_fill, _up_maker, _up_wp) + self.config.yes_extra_edge
            )

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
            _req = _required(up_fill, _up_maker)
            _regime = (
                "maker-bar (quarter fee, bid-priced)"
                if (_up_maker or _down_maker)
                else "taker-bar (fee-aware)"
            )
            reason = (
                f"{truth_kind} {_money(spot)} vs target {_money(target)} "
                f"({_money(delta)}): "
                f"fair {fair:.2f}, Kalshi up "
                f"{('%.2f' % up_ask) if up_ask is not None else '--'} / down "
                f"{('%.2f' % down_ask) if down_ask is not None else '--'} - "
                f"best edge {max(up_edge, down_edge):+.3f} under {_req:.3f}"
                f" [{_regime}]")
            if (up_fill is not None and up_fill >= self.config.max_entry_price) or (
                down_fill is not None and down_fill >= self.config.max_entry_price
            ):
                reason += " - entry price in the blocked $0.90+ band"
        else:
            reason = (
                f"{truth_kind} {_money(spot)} vs target {_money(target)} "
                f"({_money(delta)}): "
                f"fair {('%.2f' % (fair if side == 'up' else 1.0 - fair))} vs Kalshi "
                f"{float(kalshi or 0.0):.2f} on {side.upper()} - edge {edge:+.3f}"
            )
            if self._sigma_used:
                reason += f" | rv-sigma ${self._sigma_used:,.0f}"
            if self._impl_sigma:
                reason += f" | impl-sigma ${self._impl_sigma:,.0f}"
            _chosen_maker = (_up_maker if side == "up" else _down_maker)
            if _chosen_maker:
                reason += " | maker-bar (quarter fee, bid-priced)"
            if self._burst:
                reason += " | burst: taker on a fresh move (stale quote)"
            if self.config.tiered_edge_enabled and side:
                _wp = fair if side == "up" else 1.0 - fair
                if _wp >= self.config.strong_prob:
                    reason += (
                        f" | strong view ({_wp:.2f}): "
                        f"{self.config.strong_edge_scale:.2f}x edge bar"
                    )
                    if _wp >= self.config.certified_prob:
                        reason += (
                            f" | certified: {self.config.certified_mult:.1f}x clip"
                        )
                elif _wp < self.config.marginal_prob:
                    reason += (
                        f" | marginal view ({_wp:.2f}): "
                        f"{self.config.marginal_edge_scale:.2f}x edge bar"
                    )

        # Size on the price the order will actually fill at: the side's own ask.
        # UP fills at yes_ask, DOWN fills at no_ask. An earlier revision filled
        # DOWN at (1 - ask) -- the UP price for a DOWN bet -- which booked $48
        # of exposure as a $4.93 clip and credited exits at the real price: ~$43
        # of phantom profit per trade. That inversion is what flattered the DRY
        # NO line. Both sides fill at their own ask now.
        # Size on the price the entry actually pays - the maker bid when the
        # clip will rest, else the taker ask.
        fill_price = (_up_ref if side == "up" else _down_ref) if side else 0.0
        contracts = 0
        if side:
            _clip = clip_usd
            _win_side = fair if side == "up" else (1.0 - fair)
            _bal = self._live_balance if live else self._dry_cash_cache
            # FRACTIONAL KELLY: clip = balance x clip(scale x k*), k* =
            # (f - c) / (1 - c). The clip compounds with the book and widens
            # with the edge; kelly_cap bounds any single bet. This is what
            # turns $10 into something: growth-optimal sizing on every edge,
            # not a flat $5 til the account dies of fee drag.
            # Confidence-weighted Kelly: scale up for strong model conviction
            _conf_kelly_scale = (
                self.config.kelly_scale
                + self.config.kelly_scale
                * max(0.0, fair - self.config.kelly_confidence_pivot)
                / (1.0 - self.config.kelly_confidence_pivot)
            )
            _conf_kelly_scale = min(
                _conf_kelly_scale,
                self.config.kelly_scale * self.config.kelly_confidence_max_mult,
            )
            if (
                self.config.kelly_sizing
                and 0.0 < fill_price < 1.0
            ):
                # Both books use the same balance source:
                # DRIED uses the simulated cash cache, LIVE uses the real balance.
                if _bal and _bal > 0.0:
                    _kelly = (_win_side - fill_price) / (1.0 - fill_price)
                    _k = max(
                        0.0,
                        min(
                            self.config.kelly_cap,
                            _conf_kelly_scale * _kelly,
                        ),
                    )
                    _clip = round(_bal * _k, 2)
            # CONVICTION MULTIPLIER (new): the strong-view discount finds
            # these trades; this makes them PAY. A view at/above strong_prob
            # sizes up to conviction_mult x on top of Kelly. Three ceilings
            # still bound the outcome: the conviction fraction ceiling, an
            # explicit cash budget (LIVE's balance fraction), and the
            # correlated/notional caps downstream.
            # MULTI-SIGNAL CONVICTION STACKING: when 3+ independent signals
            # agree, apply the full_conviction_mult instead of the base mult.
            _signal_count = 0
            _our_dir = 1 if side == "up" else -1
            if _win_side >= self.config.strong_prob:
                _signal_count += 1
            if market.volume is not None and market.volume > self.config.volume_spike_threshold:
                _signal_count += 1
            if self._poly_window is not None:
                _poly_drift = self._poly_window.up - self._poly_window.down
                if (_our_dir > 0 and _poly_drift > 0) or (_our_dir < 0 and _poly_drift < 0):
                    _signal_count += 1
            if self._vol_adj < 0:
                _signal_count += 1
            if self._burst:
                _signal_count += 1
            _mult = self.config.conviction_mult
            if _signal_count >= 3:
                _mult = self.config.full_conviction_mult
            if _win_side >= self.config.strong_prob:
                _clip = conviction_scale(_clip, _win_side, self.config)
                if _signal_count >= 3:
                    _clip = round(_clip * (_mult / self.config.conviction_mult), 2)
                    reason += f" | multi-signal ({_signal_count}x/{_mult:.1f}x)"
            if _bal and _bal > 0.0:
                _clip = min(
                    _clip if _clip is not None else 0.0,
                    float(_bal) * self.config.conviction_max_fraction,
                )
            if clip_usd is not None:
                _clip = min(float(_clip), float(clip_usd))
            # SESSION MULTIPLIER: scale clip to US equity market liquidity.
            # High-volume session (9:30-11:30 AM ET) = tighter spreads = bigger clips.
            _sm = session_multiplier(self.config)
            if _clip is not None:
                _clip = round(_clip * _sm, 2)
                if _sm != 1.00:
                    reason += f" | session_mult {_sm:.2f}"
            contracts = self._size(fill_price, clip_usd=_clip)
        if side and contracts > 0:
            # The Hyperliquid perp hedge that flattens this clip's direction,
            # sized from the binary delta and marked against the public BTC
            # mid. Planned and logged on every entry in both books; orders on
            # HL fire only when the operator arms them (HL_HEDGE_ENABLED).
            if self.config.vol_edge_enabled and self._sigma_used:
                try:
                    from src.jobs.vol_edge import hedge_btc_qty

                    _hq, _hd = hedge_btc_qty(
                        side, contracts, delta, spot, self._sigma_used, seconds_left
                    )
                    if _hq > 0:
                        _mark = f" @ ~{self.hl.mid:,.0f}" if self.hl.mid else ""
                        reason += f" | hl-hedge: {_hd} {_hq} BTC{_mark}"
                except Exception:  # noqa: BLE001 - annotation only
                    pass
            # Same-event arb across venues (Poly leg = logged intent until a
            # CLOB executor exists).
            try:
                from src.jobs.cross_venue_poly import bucket_to_utc

                _end = bucket_to_utc(market.bucket)
                _arb = self.poly.arb_vs_kalshi(_end, market.up_price) if _end else None
                if _arb:
                    self.poly_arb_last = _arb
                    if _arb["edge"] >= 0.02:
                        reason += f" | poly arb +{_arb['edge']:.2f} ({_arb['legs']})"
            except Exception:  # noqa: BLE001 - annotation only
                pass
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
        # Exchange minimum is ONE CONTRACT, not any dollar figure. A sub-$1
        # clip must still be a whole contract (a 1-cent contract is $0.01).
        if contracts < 1:
            contracts = 1
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

        ONE clip per ticker, BOTH books: the live trade log showed every
        dollar-large loss was a pyramid - clips stacked on the same flip are
        the same bet at 3-4x the Kelly cap, and the stack dies together. The
        money rules below never fixed that, because each clip passed them
        alone while the aggregate was one oversized bet.
        """
        opposite = {"up": "NO", "down": "YES"}
        for p in held:
            if p["ticker"] == signal.ticker and p["side"] == opposite.get(signal.side):
                return (
                    f"already hold the opposite side of {signal.ticker}; "
                    "closing first (never both sides of one contract)"
                )
        if any(p["ticker"] == signal.ticker for p in held):
            return (
                f"already hold {MAX_CLIPS_PER_TICKER} clip of {signal.ticker}; "
                "one bucket is one coin flip and one Kelly-sized bet - "
                "re-betting the same flip is not a new edge"
            )

        win_prob = signal.fair if signal.side == "up" else 1.0 - signal.fair
        # A sub-0.50 win probability is a coin-flip-or-worse bet. Unless we can
        # GUARANTEE selling it back profitably BEFORE it settles a loser (which
        # a 15-minute bucket with no early-exit fill does not), taking it is just
        # paying fee on a 9% chance. The old edge-override let these through on
        # the theory that "the market is mispriced", but the live log shows them
        # held to market_resolution and lost. Refuse them outright.
        if win_prob < self.config.min_win_prob:
            self.book.skipped_low_prob += 1
            return (
                f"win probability {win_prob:.2f} below "
                f"{self.config.min_win_prob:.2f} on {signal.side.upper()}"
            )

        # Quick win mode: tight high-confidence capture for tiny accounts ($7).
        if self.config.quick_win_enabled:
            signal.notional = min(signal.notional, self.config.quick_win_max_clip_usd)
            # Quick exit targets applied externally; here we just enforce tight size.
        open_notional = sum(float(p.get("notional") or 0.0) for p in held)
        if open_notional + signal.notional > self.config.max_open_notional:
            return (
                f"${open_notional:.2f} already deployed; adding "
                f"${signal.notional:.2f} would exceed the "
                f"${self.config.max_open_notional:.2f} cap"
            )

        # CORRELATED-CRYPTO CAP (new): BTC/ETH/XRP/HYPE move together - four
        # same-direction lanes are ONE bet wearing four hats. After the
        # global cap passes, cap the total same-side up/down exposure so a
        # single reversal cannot hit every lane at once. Money risk, not
        # trade counting.
        _want = "YES" if signal.side == "up" else "NO"
        _same_side = sum(
            float(p.get("notional") or 0.0)
            for p in held
            if str(p.get("side") or "").upper() == _want
        )
        if _same_side + float(signal.notional or 0.0) > self.config.correlated_cap_usd:
            self.book.skipped_correlated += 1
            return (
                f"correlated {signal.side.upper()} exposure would reach "
                f"${_same_side + float(signal.notional or 0.0):.2f} "
                f"(cap ${self.config.correlated_cap_usd:.2f})"
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
                    _kalshi_fee = 0.07 * (1.0 - float(signal.ask or 0.5))
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

                    # Book split mandated by the operator: DRY runs the FREE
                    # OpenRouter configuration (free-tier model, `:free` slug,
                    # $0 billed), LIVE runs the HOT configuration (paid key +
                    # paid models). The DRY key is used when set; otherwise the
                    # only configured key carries the calls — a `:free` model
                    # bills nothing to it either way.
                    if live:
                        # LIVE keeps the paid default; the job roster below
                        # picks its own (paid) judge models.
                        self._ai_client = OpenRouterClient()
                    else:
                        # DRY is free-only: the `:free` model is the ONLY
                        # model this client can ever request. If it cannot,
                        # the veto simply has no opinion - never a paid call.
                        self._ai_client = OpenRouterClient(
                            api_key=settings.api.dry_openrouter_api_key or None,
                            default_model=settings.api.dry_openrouter_model,
                            free_only=True,
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
                    "required": 0.07 * (1.0 - float(signal.ask or 0.5)),
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
                    "SELECT side, pnl FROM trade_logs WHERE strategy=?"
                    " ORDER BY rowid DESC LIMIT ?",
                    (self.lane, limit),
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
        # Maker entry - both books now (THE LAW: DRY rehearses what LIVE does).
        # With time on the clock, rest the buy at the side's bid (maker fee
        # ~1/4 of taker) and wait a few seconds for the book to come to us.
        # The signal prices the edge at the ask, so a fill at the bid is
        # strictly better than the price that already cleared the bar. No
        # usable bid -> the taker path below, unchanged. In DRY the same
        # decision runs against the same real order book; execute_position
        # only books the fill if the book actually crosses the resting price,
        # so the rehearsal carries the fill risk too.
        _maker_wait = 0.0
        from src.jobs import live_fees as _lf_mk
        if _lf_mk.should_use_maker_entry(signal.seconds_left):
            try:
                from src.jobs import live_fees as _live_fees_mk
                from src.utils.market_prices import get_market_prices as _gmp

                if _live_fees_mk.should_use_maker_entry(signal.seconds_left):
                    _md = await self._client.get_market(signal.ticker)
                    _yb, _ya, _nb, _na = _gmp((_md or {}).get("market") or {})
                    _bid = _yb if signal.side == "up" else _nb
                    _mk = _live_fees_mk.maker_entry_price(signal.side, _bid, ask)
                    # Rest only into a tight book: a wide spread means the
                    # bid is a fiction and the resting order would sit there
                    # while the market leaves.
                    if (
                        _mk is not None
                        and float(ask or 0.0) > 0
                        and (float(ask) - float(_mk)) <= 0.05
                    ):
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
                f"{self.lane.upper()} UP/DOWN {signal.reason} | {signal.contracts} @ ${price:.3f} | "
                f"{signal.seconds_left}s left"
            ),
            confidence=abs(signal.edge),
            # CALIBRATION: the CHOSEN SIDE's win probability, not the up-fair.
            # Storing signal.fair stored 0.19 for a strong DOWN trade (up-fair
            # 0.19, down-fair 0.81), which inverted the report card: the
            # "below 0.55" band was secretly the strong-DOWN band. The fair in
            # the rationale ("fair 0.81 vs Kalshi ... on DOWN") is the chosen
            # side - match it.
            entry_fair=float(
                signal.fair if str(getattr(signal, "side", "")).lower() == "up"
                else 1.0 - signal.fair
            ),
            live=live,
            strategy=self.lane,
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

    async def _certain_win_pair(self, market: Any, live: bool) -> bool:
        """Buy BOTH sides when YES+NO cost less than $1.00 after both fees.

        The pair pays $1.00 at settlement whatever happens, so this is the
        only trade in the book that is arithmetic instead of prediction -
        which is why it may take a larger slice (still capped by cash and the
        notional law). Both legs are taker: resting an "instant" arb risks
        one leg never filling while the other market runs away. Every leg is
        inserted and funded through the same canonical path as any clip, so
        DRY rehearses exactly what LIVE does.
        """
        from src.jobs import certain_win as _cw
        from src.jobs.execute import execute_position
        from src.utils.database import DatabaseManager, Position

        if market is None:
            return False
        up_ask = float(getattr(market, "up_price", 0.0) or 0.0)
        down_ask = float(getattr(market, "down_price", 0.0) or 0.0)
        ok, net = _cw.evaluate(up_ask, down_ask, self.config.certain_win_min_net)
        if not ok:
            return False

        import time as _time

        now = _time.monotonic()
        if now - float(self._cw_last.get(market.ticker, 0.0)) < 120.0:
            return False
        if int(self._cw_rounds.get(market.ticker, 0)) >= 3:
            return False

        if self.db_manager is None:
            self.db_manager = DatabaseManager()
            await self.db_manager.initialize()
        if self._client is None:
            try:
                from src.clients.kalshi_client import KalshiClient

                self._client = KalshiClient()
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = f"certain_win: no Kalshi client: {exc}"
                return False

        avail = self._live_balance if live else self._dry_cash_cache
        avail = float(avail or 0.0)
        if avail <= 0.0:
            return False
        budget = min(
            self.config.certain_win_max_usd,
            avail * self.config.certain_win_cash_fraction,
            self.config.max_open_notional,
        )
        contracts = _cw.contracts_for(up_ask, down_ask, budget)
        if contracts < 1:
            return False

        placed: List[str] = []
        for side, price in (("YES", up_ask), ("NO", down_ask)):
            position = Position(
                market_id=market.ticker,
                side=side,
                entry_price=round(float(price), 4),
                quantity=contracts,
                timestamp=_utcnow(),
                rationale=(
                    f"CERTAIN-WIN pair ({self.lane.upper()}): both sides of "
                    f"{market.ticker} for {contracts} x "
                    f"${up_ask + down_ask:.3f} = ${net:.3f}/pair net after fees"
                ),
                confidence=0.99,
                live=live,
                strategy=self.lane,
                mode="live" if live else "dry",
            )
            try:
                position_id = await self.db_manager.add_position(
                    position, allow_duplicate=True
                )
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = (
                    f"certain_win insert failed: {type(exc).__name__}: {exc}"
                )
                continue
            if position_id is None:
                continue
            position.id = position_id
            try:
                filled = await execute_position(
                    position, live, self.db_manager, self._client
                )
            except Exception as exc:  # noqa: BLE001
                self.book.last_error = (
                    f"certain_win submit failed: {type(exc).__name__}: {exc}"
                )
                filled = False
            if filled:
                placed.append(side)
            else:
                try:
                    await self.db_manager.delete_position(position_id)
                except Exception:  # noqa: BLE001
                    pass

        if len(placed) == 2:
            self._cw_last[market.ticker] = now
            self._cw_rounds[market.ticker] = self._cw_rounds.get(market.ticker, 0) + 1
            self.book.certain_wins += 1
            self.book.certain_win_net += net * contracts
            return True
        if len(placed) == 1:
            # Leg risk: one side filled. Flag loudly; tracking manages the
            # single leg like any other position.
            self.book.last_error = (
                f"certain_win LEG RISK: only {placed[0]} filled on {market.ticker}"
            )
        return False

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
        if not live:
            # The DRY book's own cash, for the Kelly sizer - same rule as
            # LIVE, on its own money.
            try:
                _acct = await TradingMode(db_path=self._db_path()).dry_account()
                self._dry_cash_cache = float((_acct or {}).get("cash") or 0.0)
            except Exception:  # noqa: BLE001 - fallback clip covers it
                self._dry_cash_cache = None

        # Refresh the cross-venue consensus once per pass; the (sync) scorer
        # only ever reads the cached snapshot. Failures degrade to no guard.
        if self.config.venue_guard_enabled:
            try:
                await self.venue_guard.refresh()
            except Exception:  # noqa: BLE001
                pass
        if self.config.poly_guard_enabled:
            try:
                await self.poly.refresh()
            except Exception:  # noqa: BLE001
                pass
        try:
            await self.hl.refresh()
        except Exception:  # noqa: BLE001
            pass

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
            # Fetch throttle: far from expiry the book barely moves,
            # so reuse a fresh cache instead of hammering /markets into 429s.
            # Applied identically to BOTH books.
            _use_cache = False
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
            # 429 resilience: on rate-limit, reuse the last good
            # cache with jittered backoff instead of blanking the cycle.
            # Applied identically to BOTH books.
            _msg = f"{type(exc).__name__}: {exc}"
            _is_429 = "429" in _msg or "Too Many Requests" in _msg
            if _is_429 and getattr(self.feed, "markets", None):
                try:
                    import asyncio as _asyncio

                    from src.jobs import live_fees as _live_fees

                    await _asyncio.sleep(_live_fees.backoff_delay_seconds(1))
                except Exception:  # noqa: BLE001
                    pass
                self.book.last_error = f"series fetch 429 (cache reused): {_msg[:120]}"
            else:
                self.book.last_error = f"series fetch: {_msg}"
                return self.book.summary()

        market = self.feed.nearest()

        # Sizes the clip against the real balance, so the account keeps
        # trading on every edge instead of spending itself dark in one or two
        # clips. A read failure must not stop the cycle - it falls back to the
        # fixed clip and lets execute_position's own fail-closed balance check
        # refuse if the account genuinely cannot pay.
        live_budget: Optional[float] = None
        variance_mult: float = 1.0
        try:
            if self._client is None:
                from src.clients.kalshi_client import KalshiClient

                self._client = KalshiClient()
            bal = await self._client.get_balance()
            cents = float((bal or {}).get("balance") or 0.0)
            if cents < 1:
                # Truly zero - one contract at any price costs at least a cent.
                self.book.last_error = (
                    f"balance ${cents / 100.0:.4f} is empty - nothing can be "
                    f"bought until the account holds at least one cent"
                )
                live_budget = None
            else:
                # A tiny account still trades: clip = balance x fraction, but
                # never smaller than one whole contract (downstream _size and
                # broker enforce the 1-contract minimum).
                live_budget = max(
                    round(cents / 100.0 * self.config.live_cash_fraction, 2),
                    _min_viable_notional(cents),
                )
                # Raw balance for the Kelly sizer in evaluate().
                self._live_balance = cents / 100.0
                self._dry_cash_cache = cents / 100.0
                # === INVENTION #4: Drawdown-Aware Dynamic Kelly ===
                # Track peak balance; shrink Kelly if drawdown > peak_floor.
                # Only applies after a peak has been established (i.e., not the
                # first cycle, when peak==current so drawdown=0).
                if self._peak_balance is None or self._live_balance > self._peak_balance:
                    self._peak_balance = self._live_balance
                if (
                    self.config.drawdown_kelly_enabled
                    and self._peak_balance is not None
                    and self._peak_balance > 0
                    and self._peak_balance > self._live_balance
                ):
                    from src.jobs.risk_overlays import (
                        DrawdownFraction,
                        kelly_scale_for_drawdown,
                    )
                    _dd = DrawdownFraction(
                        peak=self._peak_balance, current=self._live_balance
                    )
                    if _dd.fraction() >= self.config.drawdown_peak_floor:
                        # Past drawdown floor: refuse new entries until recovery.
                        self.book.skipped_drawdown += 1
                        self.book.last_error = (
                            f"drawdown floor: {_dd.fraction():.1%} >= "
                            f"{self.config.drawdown_peak_floor:.0%} - no entries"
                        )
                        return self.book.summary()
                    _dd_scale = kelly_scale_for_drawdown(
                        base_fraction=1.0,
                        drawdown=_dd,
                        peak_floor=self.config.drawdown_peak_floor,
                    )
                    live_budget = round(live_budget * _dd_scale, 2)
        except Exception as exc:  # noqa: BLE001
            self.book.last_error = f"balance read: {type(exc).__name__}: {exc}"

        # === INVENTION #11: Volatility-Targeted Sizing ===
        # Scale notional to fixed target_vol - shrinks when realized is hotter.
        if self.config.vol_target_enabled and self._sigma_used:
            try:
                from src.jobs.risk_overlays import vol_target_scale

                _spot = float(self.spot.price or 0.0)
                if _spot > 0:
                    _vol_realized = float(self._sigma_used) / _spot
                    _vt_scale = vol_target_scale(
                        realized_vol=_vol_realized,
                        target_vol=self.config.vol_target_pct,
                    )
                    if live_budget is not None:
                        live_budget = round(live_budget * _vt_scale, 2)
                    self._realized_vol = _vol_realized
            except Exception:  # noqa: BLE001 - vol target never blocks
                pass

        # CERTAIN-WIN PAIR: a binary contract's two sides pay $1.00 together.
        # When both asks plus both taker fees leave a net, buying BOTH is the
        # one trade here that is not a prediction - sized larger than an edge
        # clip because it cannot lose, still capped by cash and the notional
        # law. Fired before scoring: it needs no view on anything.
        if self.config.certain_win_enabled and market is not None:
            try:
                if await self._certain_win_pair(market, live):
                    return self.book.summary()
            except Exception as exc:  # noqa: BLE001 - never block the cycle
                self.book.last_error = f"certain_win: {type(exc).__name__}: {exc}"

        # Variance-commensurate sizing: the bigger Kalshi's lie
        # (spot far from target with minutes to close), the bigger the clip
        # -- up to 2.5x -- because convergence is proportionally more
        # certain. Bounded by the balance fraction, the 1-contract minimum
        # and max_open_notional downstream. Applied to BOTH books.
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
                        float(_tgt) * self.config.noise_pct,
                    )
                    live_budget = round(live_budget * variance_mult, 2)
            except Exception:  # noqa: BLE001 - sizing never blocks entry
                variance_mult = 1.0
        # SURVIVAL / HYPER-CAUTION: below the floor the book is one loss from
        # being unable to trade at all, so this cycle demands near-certain wins
        # and sizes down. Applied to the LIVE book only (a DRIED book is
        # expendable rehearsal cash, not the real account that must survive).
        survival = live and self.config.survival_enabled and self._live_balance is not None and (
            self._live_balance <= self.config.survival_floor_usd
        )
        _saved_edge = self.config.min_edge
        _saved_win = self.config.min_win_prob
        if survival:
            self.config.min_edge = max(self.config.min_edge, self.config.survival_min_edge)
            self.config.min_win_prob = max(self.config.min_win_prob, self.config.survival_min_win_prob)
            # Hyper-caution: require a near-certain win, but do NOT flat-cap the
            # bet to a fixed 10%. The Kelly engine already sizes the clip by the
            # edge (near-certain -> bigger), so a flat cap just shrinks the exact
            # winners we want to size up on. Only guard against a single clip
            # ending the account: never more than the survival fraction, but still
            # proportional to the balance so a genuine edge earns real money.
            _surv_cap = max(self._live_balance * self.config.survival_clip_fraction, 0.01)
            if live_budget is not None:
                live_budget = min(live_budget, self._live_balance)
            self._live_balance = min(self._live_balance, _surv_cap)
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
                    if (p.strategy or "") != self.lane:
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
            # SURVIVAL bars must also govern the entry guard (min_win_prob lives
            # there too), so re-raise them for the guard and restore after.
            if survival:
                self.config.min_win_prob = max(self.config.min_win_prob, self.config.survival_min_win_prob)
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
        # No time-of-day session bump: a "losing hour" edge surcharge is an
        # arbitrary lock that a minute later is just an impediment.
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
                       if (signal and (live or self.config.dry_fee_enabled)) else 0.0)
                    + _session_bump,
                    4,
                ),
                "reason": (signal.reason if signal else refusal) or "no quotable contract",
            }
        )
        # Restore the survival-adjusted bars so the next cycle starts clean.
        if survival:
            self.config.min_edge = _saved_edge
            self.config.min_win_prob = _saved_win
        return result


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _lane_name() -> str:
    """This process's own strategy identity.

    The dashboard stamps STRATEGY_NAME on every spawned child (btc_updown,
    xrp_updown, btc_1h_updown). The four lanes run the same
    trader but each must attribute its positions, its veto streaks and its
    heartbeat to its OWN row - a hardcoded "btc_updown" made every crypto lane
    share one position table, which is why XRP/XAU/ETH never showed their own
    trades. A manual run without the stamp falls back to btc_updown.
    """
    import os

    return os.environ.get("STRATEGY_NAME", "").strip() or "btc_updown"


async def run_updown_trader(
    config: Optional[UpDownConfig] = None,
    loop: bool = False,
    interval: float = 0.0,
    series: str = "KXXRP15M",
    spot_product: str = "XRP-USD",
) -> None:
    """Run the up/down trader, optionally on a loop until interrupted.

    `series` is the Kalshi series ticker (e.g. KXXRP15M, KXGOLD15M, KXBTC15M, KXETH15M).
    `spot_product` is the Coinbase pair (e.g. XRP-USD, XAU-USD, BTC-USD, ETH-USD).
    """
    from src.jobs.market_data import MarketDataHub

    config = config or UpDownConfig()
    # Grade each lane against its own asset, not BTC's defaults.
    config.apply_asset(spot_product)
    hub = MarketDataHub(spot_product=spot_product, series=series)
    # BRTI truth: Kalshi's own settlement index over its own socket. Starts
    # degraded without keys and the trader falls back to Coinbase; never fatal.
    # DRY no longer uses BRTI — both books use Coinbase spot only.
    brti = None
    try:
        from src.jobs.brti_feed import BrtiFeed

        brti = BrtiFeed()
    except Exception as exc:  # noqa: BLE001
        print(f"{series}: BRTI feed unavailable ({exc}); coinbase-spot only", flush=True)
        brti = None
    trader = UpDownTrader(
        hub.spot,
        hub.feed,
        config,
        brti=brti,
        lane=_lane_name(),
    )
    sleep_for = interval or config.poll_seconds or 4.0
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
                import os

                from src.utils.strategy_runtime import StrategyRuntime

                # This process's own lane and book. The dashboard
                # stamps both on every child it spawns: the three
                # UP/DOWN lanes run the same trader, so a hardcoded
                # name put every lane's heartbeat on the first
                # lane's row and the other two cards read "stopped"
                # while their processes traded. The book word is the
                # runtime vocabulary ("paper"/"live") the row was
                # recorded under; a manual run has no stamp and
                # falls back to the live switch.
                lane = getattr(trader, "lane", None) or _lane_name()
                book_word = os.environ.get("STRATEGY_BOOK_MODE", "").strip().lower()
                if book_word not in ("paper", "live"):
                    from src.jobs.broker import should_trade_live

                    book_word = "live" if should_trade_live() else "paper"
                await StrategyRuntime(db_path=trader._db_path()).record_heartbeat(
                    lane, book_word
                )
            except Exception as exc:  # noqa: BLE001 - proof must not kill the loop
                print(f"BTC 15m: heartbeat write failed: {type(exc).__name__}: {exc}", flush=True)

            # Book reaper: every 15 minutes, kill local rows Kalshi
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
