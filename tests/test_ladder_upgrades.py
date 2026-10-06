"""The honest fair value, the venue guard, and the DRY maker mirror.

Three upgrades, one law: DRY rehearses exactly what LIVE does.

1. Fair value now prices the binary as a diffusion with MEASURED realized
   volatility, so "spot moved $50 but the contract moved $30" is geometry the
   bot understands instead of a fixed-band guess.
2. A one-venue dislocation guard refuses entries when the other major USD
   venues sit on the opposite side of the target: Coinbase alone is not the
   market Kalshi settles on.
3. DRY runs the same maker entry LIVE runs - same wait, same real book, and a
   fill only when the book actually crosses the resting price.
"""
import math
import time

import pytest

from src.jobs.ladder_trader import (
    UpDownConfig,
    UpDownMarket,
    UpDownTrader,
    fair_up_probability,
)
from src.jobs.market_data import Btc15mFeed, SpotFeed
from src.jobs.realized_vol import RealizedVol, gaussian_cdf


# ---------------------------------------------------------------------------
# Realized volatility
# ---------------------------------------------------------------------------
def test_realized_vol_needs_tape_before_it_speaks():
    rv = RealizedVol()
    assert rv.sigma_dollars(900.0, last_price=84000.0) is None
    for i in range(10):
        rv.observe(84000.0 + i, now=i * 5.0)
    assert rv.sigma_dollars(900.0, last_price=84000.0 + 10) is None


def test_realized_vol_measures_a_noisy_trend():
    """A drifting tape with alternating noise yields a bounded, sqrt-scaled sigma."""
    rv = RealizedVol()
    # Ramp +$0.10 per print with +/- $1 alternating noise, one print per 5s.
    for i in range(60):
        rv.observe(84000.0 + 0.1 * i + (1.0 if i % 2 else -1.0), now=i * 5.0)
    s15 = rv.sigma_dollars(900.0, last_price=84005.9)
    assert s15 is not None
    assert 5.0 <= s15 <= 100.0
    # Volatility scales with the square root of the horizon.
    s60 = rv.sigma_dollars(3600.0, last_price=84005.9)
    assert s60 == pytest.approx(s15 * 2.0, rel=0.05)


def test_realized_vol_ignores_echoing_prints():
    """A flat feed that repeats one price must NOT read as zero volatility."""
    rv = RealizedVol()
    for i in range(120):
        rv.observe(84000.0, now=i * 2.0)
    assert rv.sigma_dollars(900.0, last_price=84000.0) is None


def test_realized_vol_survives_bad_prints():
    rv = RealizedVol()
    rv.observe(None)
    rv.observe("abc")
    rv.observe(-5)
    rv.observe(0)
    assert rv.sigma_dollars(900.0) is None


def test_gaussian_cdf_is_the_normal_curve():
    assert gaussian_cdf(0.0) == pytest.approx(0.5)
    assert gaussian_cdf(1.96) == pytest.approx(0.975, abs=0.001)
    assert gaussian_cdf(-1.96) == pytest.approx(0.025, abs=0.001)


# ---------------------------------------------------------------------------
# The diffusion fair value
# ---------------------------------------------------------------------------
def test_sigma_fair_value_is_half_at_the_target():
    assert fair_up_probability(84609.34, 84609.34, sigma_dollars=200.0) == 0.5


def test_sigma_fair_value_rises_and_falls_with_distance():
    p_up = fair_up_probability(84800, 84609.34, sigma_dollars=200.0)
    p_down = fair_up_probability(84400, 84609.34, sigma_dollars=200.0)
    assert p_up > 0.8
    assert p_down < 0.2
    # Monotone in the distance.
    assert fair_up_probability(84700, 84609.34, sigma_dollars=200.0) < p_up


def test_less_time_makes_the_same_distance_more_decisive():
    """The operator's observation, formalized: the same $50 means less with
    a full window left and nearly everything with seconds to go."""
    far = fair_up_probability(84659.34, 84609.34, sigma_dollars=200.0, seconds_left=890.0)
    near = fair_up_probability(84659.34, 84609.34, sigma_dollars=200.0, seconds_left=15.0)
    assert near > far
    assert far > 0.5 > 0.0


def test_higher_volatility_pulls_the_price_toward_half():
    calm = fair_up_probability(84700, 84609.34, sigma_dollars=80.0)
    wild = fair_up_probability(84700, 84609.34, sigma_dollars=800.0)
    assert calm > wild > 0.5


def test_legacy_logistic_unchanged_without_sigma():
    """The cold-start path is byte-identical to the old model."""
    assert fair_up_probability(84609.34, 84609.34) == 0.5
    assert fair_up_probability(84800, 84609.34) > 0.9
    assert fair_up_probability(84400, 84609.34) < 0.1


# ---------------------------------------------------------------------------
# The trader wiring
# ---------------------------------------------------------------------------
def _dollars(**kw):
    return UpDownMarket(
        ticker=kw.pop("ticker", "KXBTC15M-26OCT011715-15"),
        event_ticker="KXBTC15M-26OCT011715",
        bucket="26OCT011715",
        horizon=15,
        title="BTC price up in next 15 mins?",
        target=kw.pop("target", 84609.34),
        close_ts=time.time() + kw.pop("seconds_left", 600),
        **kw,
    )


def _trader(db=None):
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = time.time()
    spot.source = "test"
    trader = UpDownTrader(spot, Btc15mFeed(), UpDownConfig())
    if db is not None:
        trader.db_manager = db
    return trader


def test_trader_records_the_sigma_it_used():
    """The entry log must say which volatility priced the trade."""
    trader = _trader()
    t0 = time.monotonic()
    for i in range(60):
        trader.rv.observe(84000.0 + 0.1 * i + (1.0 if i % 2 else -1.0), now=t0 - 300.0 + i * 5.0)
    market = _dollars(
        seconds_left=300, yes_bid=0.40, yes_ask=0.41, no_bid=0.59, no_ask=0.60
    )
    signal = trader.evaluate(market, live=False)
    assert signal is not None
    assert signal.side, f"expected a trade, got: {signal.reason}"
    assert "rv-sigma" in signal.reason


def test_venue_guard_vetoes_a_one_venue_move():
    """The others contradict Coinbase beyond budget -> no trade."""
    trader = _trader()
    market = _dollars(
        seconds_left=120,
        target=84609.34,
        yes_bid=0.30,
        yes_ask=0.31,
        no_bid=0.69,
        no_ask=0.70,
    )
    # Coinbase is +$290 over target; the other venues sit $120 UNDER it.
    trader.venue_guard._prices = {"kraken": 84400.0, "bitstamp": 84420.0}
    trader.venue_guard._updated = time.monotonic()
    signal = trader.evaluate(market, live=False)
    assert signal is not None
    assert signal.side == ""
    assert "venue guard" in signal.reason
    assert trader.book.skipped_venue_guard == 1


def test_venue_guard_stays_silent_on_normal_basis():
    """A small cross-venue gap is normal basis, not a veto."""
    trader = _trader()
    market = _dollars(
        seconds_left=120,
        target=84609.34,
        yes_bid=0.30,
        yes_ask=0.31,
        no_bid=0.69,
        no_ask=0.70,
    )
    # Others $10 lower: inside the $60 budget.
    trader.venue_guard._prices = {"kraken": 84890.0, "bitstamp": 84880.0}
    trader.venue_guard._updated = time.monotonic()
    signal = trader.evaluate(market, live=False)
    assert trader.book.skipped_venue_guard == 0


def test_venue_guard_disabled_by_config():
    trader = _trader()
    trader.config.venue_guard_enabled = False
    market = _dollars(
        seconds_left=120,
        target=84609.34,
        yes_bid=0.30,
        yes_ask=0.31,
        no_bid=0.69,
        no_ask=0.70,
    )
    trader.venue_guard._prices = {"kraken": 84400.0, "bitstamp": 84420.0}
    trader.venue_guard._updated = time.monotonic()
    trader.evaluate(market, live=False)
    assert trader.book.skipped_venue_guard == 0


def test_venue_guard_degrades_to_noop_when_feeds_are_dark():
    """No readings -> no guard. Its own outage must never block the book."""
    trader = _trader()
    assert trader.venue_guard.vetoes("up", 290.0, 84900.0) == (False, "")
    assert trader.venue_guard.consensus(84900.0) is None
    assert trader.venue_guard.dislocation(84900.0) is None


# ---------------------------------------------------------------------------
# The DRY maker mirror
# ---------------------------------------------------------------------------
class _FakeKalshi:
    def __init__(self, asks):
        self._asks = list(asks)
        self.calls = 0

    async def get_market(self, market_id):
        self.calls += 1
        ask = self._asks[min(self.calls - 1, len(self._asks) - 1)]
        return {
            "market": {
                "yes_bid_dollars": round(ask - 0.01, 2),
                "yes_ask_dollars": ask,
                "no_bid_dollars": round(1.0 - ask - 0.01, 2),
                "no_ask_dollars": round(1.0 - ask, 2),
            }
        }


async def test_dry_maker_fill_when_the_book_crosses():
    from src.jobs.broker import OrderRequest
    from src.jobs.execute import _simulate_dry_maker_fill

    req = OrderRequest(
        ticker="KXBTC15M-26OCT011715-15",
        client_order_id="t1",
        side="yes",
        action="buy",
        count=10,
        type_="limit",
        notional=4.2,
        fill_price=0.42,
    )
    # The ask walks down into our resting bid.
    client = _FakeKalshi([0.45, 0.44, 0.42])
    assert await _simulate_dry_maker_fill(client, req, None, 30.0, None) is True


async def test_dry_maker_stays_unfilled_when_the_book_never_crosses():
    from src.jobs.broker import OrderRequest
    from src.jobs.execute import _simulate_dry_maker_fill

    req = OrderRequest(
        ticker="KXBTC15M-26OCT011715-15",
        client_order_id="t2",
        side="yes",
        action="buy",
        count=10,
        type_="limit",
        notional=4.2,
        fill_price=0.42,
    )
    client = _FakeKalshi([0.45, 0.45, 0.45])
    assert await _simulate_dry_maker_fill(client, req, None, 0.0, None) is False


def test_dry_maker_entry_config_has_an_env_kill_switch(monkeypatch):
    monkeypatch.setenv("DRY_MAKER_ENTRY", "0")
    cfg = UpDownConfig()
    assert cfg.dry_maker_entry is False
    monkeypatch.delenv("DRY_MAKER_ENTRY")
    assert UpDownConfig().dry_maker_entry is True


def test_venue_guard_config_env_overrides(monkeypatch):
    monkeypatch.setenv("VENUE_GUARD", "0")
    monkeypatch.setenv("VENUE_GUARD_MAX_USD", "25")
    cfg = UpDownConfig()
    assert cfg.venue_guard_enabled is False
    assert cfg.venue_guard_max_usd == 25.0
