"""Sizing must use the price the order fills at.

The 15-minute trader sizes a clip as `notional_usd / ask`, but a DOWN clip buys
NO and therefore fills at `1 - ask`. Sizing off the cheap side of the book and
then filling on the expensive side multiplied the exposure by up to ~16x:

    ask = 0.061 (NO ask)  ->  contracts = 5 / 0.061 = 81
    fill price = 1 - 0.061 = 0.939
    cost = 81 * 0.939 = $76.07   for a clip meant to be $5

On the live DRY book that single mis-sized clip bought 81 contracts at $0.939,
was credited back $2.47 on exit, and accounts for the entire -$73.59: the
$300 simulated account went to -$73.36 and every realised figure on the page
stopped agreeing with the others.
"""
import pytest

from src.jobs.ladder_trader import UpDownConfig, UpDownSignal, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket


def _trader():
    spot = SpotFeed()
    spot.price = 84900.0
    import time as _t

    spot.ts = _t.time()
    spot.source = "test"
    feed = Btc15mFeed()
    return UpDownTrader(spot, feed, UpDownConfig())


def _market(no_ask=0.061, no_bid=0.059):
    return UpDownMarket(
        ticker="KXBTC15M-26OCT022345-45",
        event_ticker="KXBTC15M-26OCT022345",
        title="BTC up or down",
        bucket="26OCT022345",
        horizon=45,
        yes_bid=1 - no_ask,
        yes_ask=1 - no_bid,
        no_bid=no_bid,
        no_ask=no_ask,
        target=84578.0,
        close_ts=_NOW + 240,
    )


import time as _time

_NOW = _time.time()


def test_a_down_clip_costs_what_it_reports():
    """The notional a signal advertises must equal what the fill costs."""
    trader = _trader()
    signal = trader.evaluate(_market())
    assert signal is not None
    if signal.side == "down" and signal.contracts:
        fill_price = 1.0 - float(signal.ask)
        assert signal.notional == pytest.approx(signal.contracts * fill_price, abs=0.01)
        # And it must respect the configured clip size.
        assert signal.notional <= trader.config.notional_usd * 1.5


def test_cheap_asks_do_not_multiply_exposure():
    """A 6c ask must not produce 81 contracts of a 0.939 fill."""
    trader = _trader()
    contracts = trader._size(1.0 - 0.061)
    assert contracts * 0.939 == pytest.approx(trader.config.notional_usd, rel=0.35)
    assert contracts < 20, "a $5 clip must not become 80+ contracts"


def test_sizing_is_monotonic_in_price():
    """Higher fill price means fewer contracts - the whole point."""
    trader = _trader()
    cheap = trader._size(0.10)
    dear = trader._size(0.90)
    assert dear < cheap
    assert dear * 0.90 <= trader.config.notional_usd * 1.5


@pytest.mark.parametrize("price", [0.0, -0.5, 1.5, 2.0, None])
def test_unusable_prices_size_to_zero(price):
    assert _trader()._size(price) == 0


def test_a_full_price_fill_is_still_exactly_the_clip():
    """$1.00 is a real price: 5 contracts is precisely a $5 clip."""
    trader = _trader()
    assert trader._size(1.0) == 5


def test_a_signal_never_advertises_a_clip_larger_than_configured():
    """End to end: whatever the scorer emits must respect the clip size."""
    for no_ask in (0.01, 0.05, 0.2, 0.5, 0.9):
        trader = _trader()
        signal = trader.evaluate(_market(no_ask=no_ask))
        if signal is None or not signal.side or not signal.contracts:
            continue
        fill = signal.ask if signal.side == "up" else 1.0 - float(signal.ask)
        assert signal.contracts * fill <= trader.config.notional_usd * 1.5, (
            f"no_ask={no_ask} produced ${signal.contracts * fill:.2f} "
            f"for a ${trader.config.notional_usd:.2f} clip"
        )