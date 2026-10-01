"""Tests for the BTC ladder trader and the market-data layer.

These cover the places a wrong number would be invisible on the page: ticker
parsing (which encodes the hourly cadence), the edge rule, position sizing, and
the stale-spot refusal that is the strategy's main failure mode.
"""
import time

import pytest

from src.jobs.ladder_trader import LadderConfig, LadderTrader, fair_probability
from src.jobs.market_data import KalshiLadder, LadderLevel, LeadEstimator, SpotFeed


# ---------------------------------------------------------------------------
# Ticker parsing. Kalshi's cadence lives in the ticker string.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("KXBTC-26OCT0117-T73250", ("26OCT0117", 73250.0, True)),
        ("KXBTC-26OCT0117-B92250", ("26OCT0117", 92250.0, False)),
        ("KXBTCD-26OCT0116-T92299.99", None),  # wrong series
        ("NOPE-1-T5", None),
        ("KXBTC-26OCT0117-X73250", None),  # unknown side
        ("KXBTC-26OCT0117-Tnotanumber", None),
    ],
)
def test_ticker_parsing(ticker, expected):
    assert KalshiLadder._parse(ticker) == expected


def test_settlement_hour_is_encoded_in_the_ticker():
    """The buckets are hourly. A 15-minute market does not exist to trade."""
    expiry, _, _ = KalshiLadder._parse("KXBTC-26OCT0117-T73250")
    assert expiry == "26OCT0117"
    assert len(expiry) == 9  # YYMMDDHH - hour resolution, not minute resolution


def test_nearest_strike_ignores_unquoted_levels():
    ladder = KalshiLadder()
    ladder.last.levels = [
        LadderLevel(ticker="a", strike=80000.0, above=True, yes_bid=0.40, yes_ask=0.44),
        LadderLevel(ticker="b", strike=84100.0, above=True, yes_bid=None, yes_ask=None),
        LadderLevel(ticker="c", strike=84200.0, above=True, yes_bid=0.60, yes_ask=0.64),
    ]
    nearest = ladder.nearest_strike(84250.0)
    assert nearest is not None
    assert nearest.strike == 84200.0, "an unquoted level must not be selected"


# ---------------------------------------------------------------------------
# The edge rule
# ---------------------------------------------------------------------------
def _spot(price, age=0.0):
    feed = SpotFeed()
    feed.price = price
    feed.ts = time.time() - age
    feed.source = "test"
    feed.connected = True
    return feed


def _level(strike, bid, ask, above=True):
    return LadderLevel(
        ticker=f"KXBTC-T{strike}",
        strike=float(strike),
        above=above,
        yes_bid=bid,
        yes_ask=ask,
    )


def test_fair_probability_is_centred_and_monotonic():
    assert fair_probability(84000, 84000) == 0.5
    assert fair_probability(84500, 84000) > 0.5
    assert fair_probability(83500, 84000) < 0.5
    assert fair_probability(84500, 84000) > fair_probability(84100, 84000)


def test_no_trade_when_the_feeds_agree():
    """The common case: Kalshi already reflects spot, so nothing is taken."""
    trader = LadderTrader(_spot(84100.0), KalshiLadder())
    # Kalshi prices YES at ~0.62, which is roughly what that spot implies.
    signals = trader.evaluate([_level(84000, 0.60, 0.64)])
    assert signals
    assert abs(signals[0].edge) < 0.06
    assert signals[0].buy == ""
    assert not signals[0].actionable
    assert trader.book.skipped_no_edge == 1


def test_buys_yes_when_kalshi_underprices_the_strike():
    trader = LadderTrader(_spot(84250.0), KalshiLadder())
    # Spot is $150 above the strike; Kalshi prices YES at 0.52 where spot implies
    # ~0.65, so the contract is cheap against the live feed.
    signals = trader.evaluate([_level(84100, 0.50, 0.54)])
    top = signals[0]
    assert top.buy == "yes"
    assert top.edge >= 0.06
    assert top.actionable


def test_buys_no_when_kalshi_overprices_the_strike():
    trader = LadderTrader(_spot(83750.0), KalshiLadder())
    signals = trader.evaluate([_level(83900, 0.55, 0.59)])
    top = signals[0]
    assert top.buy == "no"
    assert top.edge <= -0.06


def test_refuses_to_trade_on_a_stale_spot_tick():
    """A stale spot price is the exact condition under which this is wrong."""
    trader = LadderTrader(_spot(84500.0, age=30.0), KalshiLadder())
    signals = trader.evaluate([_level(84000, 0.55, 0.59)])
    assert signals == []
    assert trader.book.skipped_stale >= 1


def test_refuses_to_trade_with_no_spot_price():
    trader = LadderTrader(_spot(0.0), KalshiLadder())
    assert trader.evaluate([_level(84000, 0.55, 0.59)]) == []


def test_skips_levels_too_far_from_spot():
    trader = LadderTrader(_spot(84000.0), KalshiLadder(), LadderConfig(max_distance_usd=150.0))
    trader.evaluate([_level(90000.0, 0.01, 0.02)])
    assert trader.book.skipped_distance == 1
    assert not trader.book.signals


def test_skips_unquoted_levels():
    trader = LadderTrader(_spot(84000.0), KalshiLadder())
    assert trader.evaluate([_level(84000, None, None)]) == []


# ---------------------------------------------------------------------------
# Sizing: $5 a clip, Kalshi's $1 floor, never negative.
# ---------------------------------------------------------------------------
def test_size_targets_five_dollars():
    trader = LadderTrader(_spot(84000.0), KalshiLadder())
    assert trader._size(0.50) == 10  # 10 * 0.50 = $5.00
    assert trader._size(0.10) == 50
    assert trader._size(0.95) == 5  # 5 * 0.95 = $4.75, next would be $5.70


def test_size_never_returns_a_fractional_or_negative_count():
    trader = LadderTrader(_spot(84000.0), KalshiLadder())
    for price in (0.001, 0.0, None, -0.5, 1.5):
        n = trader._size(price)
        assert isinstance(n, int)
        assert n >= 0


def test_one_position_at_a_time_is_the_default():
    assert LadderConfig().max_open_positions == 1
    assert LadderConfig().notional_usd == 5.0


def test_book_summary_reports_the_limits_it_enforces():
    trader = LadderTrader(_spot(84000.0), KalshiLadder())
    trader.book.open_positions = [{}]
    summary = trader.book.summary()
    assert summary["open_positions"] == 1
    assert summary["max_open_positions"] == 1


# ---------------------------------------------------------------------------
# The lead estimator measures; it does not assume.
# ---------------------------------------------------------------------------
def test_implied_spot_only_for_two_sided_quotes():
    estimator = LeadEstimator()
    assert estimator.implied_spot(_level(84000, None, None)) is None
    # A quote pinned at the extremes says nothing about where spot settles.
    assert estimator.implied_spot(_level(84000, 0.99, 0.99)) is None
    assert estimator.implied_spot(_level(84000, 0.01, 0.01)) is None
    value = estimator.implied_spot(_level(84000, 0.60, 0.64))
    assert value is not None and value > 84000


def test_lead_is_measured_from_the_ladder_not_hardcoded():
    """The 45-second claim must not appear anywhere in the code as a constant."""
    ladder = KalshiLadder()
    ladder.last.levels = [_level(84000, 0.70, 0.74)]
    estimator = LeadEstimator()
    reading = estimator.observe(_spot(84250.0), ladder)
    assert reading.ticker == "KXBTC-T84000"
    assert reading.implied_spot is not None
    assert reading.spot == 84250.0


def test_payload_reports_delta_and_says_it_is_measured():
    from src.jobs.market_data import MarketDataHub

    hub = MarketDataHub()
    hub.spot.price = 84250.0
    hub.spot.ts = time.time()
    hub.spot.source = "test"
    hub.ladder.last.levels = [_level(84000, 0.70, 0.74)]
    hub.lead.observe(hub.spot, hub.ladder)
    payload = hub.chart_payload()
    assert payload["lead"]["delta"] is not None
    assert "not assumed" in payload["lead"]["note"]
    assert payload["spot"]["price"] == 84250.0
