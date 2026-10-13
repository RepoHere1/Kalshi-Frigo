"""Wiring tests: pair legs, market-making payloads, and context into the trader."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.jobs import certain_win as cw
from src.jobs import market_context as mc
from src.jobs.ladder_trader import UpDownConfig, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket
from src.strategies import market_making as mm


def test_pair_legs_never_exceed_the_one_dollar_order_cap():
    assert cw.contracts_within_leg_cap(0.45, 0.45, 24.75) == 2  # 2 x 0.45 <= 1.00
    assert cw.contracts_within_leg_cap(0.30, 0.60, 24.75) == 1  # 0.60 leg caps at 1
    assert cw.contracts_within_leg_cap(0.05, 0.90, 100.0) == 1  # 0.90 leg caps at 1
    assert cw.contracts_within_leg_cap(0.45, 0.45, 0.50) == 0  # budget buys no pair


def test_market_making_reads_the_wrapped_kalshi_market_payload():
    wrapped = {"market": {"yes_bid_dollars": "0.41", "no_bid_dollars": "0.55"}}
    assert mm.normalize_market_payload(wrapped) == {
        "yes_bid_dollars": "0.41",
        "no_bid_dollars": "0.55",
    }
    flat = {"yes_bid_dollars": "0.41"}
    assert mm.normalize_market_payload(flat) == flat


def _spot(price=85030.0):
    spot = SpotFeed()
    spot.price = price
    spot.ts = time.time()
    spot.source = "test"
    return spot


def _market():
    return UpDownMarket(
        ticker="KXBTC15M-26OCT1300-T85000",
        event_ticker="KXBTC15M-26OCT1300",
        bucket="26OCT1300",
        horizon=15,
        title="BTC up 15m",
        target=85000.0,
        close_ts=time.time() + 600,
        yes_bid=0.49,
        yes_ask=0.50,
        no_bid=0.48,
        no_ask=0.50,
    )


class _Ctx:
    def __init__(self, fair_delta=0.0, edge=0.0, size=1.0, tier=None, text=""):
        self.value = mc.ContextState(True, fair_delta, edge, size, tier, text)

    def state(self, *_args, **_kwargs):
        return self.value


def _trader(ctx):
    trader = UpDownTrader(_spot(), Btc15mFeed(series="KXBTC15M"), UpDownConfig())
    trader._context = ctx
    return trader


def test_context_fair_delta_reaches_the_scored_signal():
    base = _trader(_Ctx()).evaluate(_market(), live=False)
    tilted = _trader(_Ctx(fair_delta=0.02, text=" | context: test tilt"))
    signal = tilted.evaluate(_market(), live=False)
    assert signal.fair == pytest.approx(base.fair + 0.02, abs=1e-4)
    assert "context: test tilt" in signal.reason


def test_context_edge_and_size_are_applied_by_the_trader():
    base = _trader(_Ctx())
    base_signal = base.evaluate(_market(), live=False)
    risky = _trader(_Ctx(edge=0.03, size=0.5))
    signal = risky.evaluate(_market(), live=False)
    assert risky._ctx_edge_add == pytest.approx(0.03)
    assert risky._ctx_size_mult == pytest.approx(0.5)
    assert signal.contracts <= base_signal.contracts


def test_high_impact_window_blocks_a_new_certain_win_pair():
    trader = _trader(_Ctx(tier="high"))
    pair = SimpleNamespace(ticker="KXBTC15M-26OCT1300-T85000", up_price=0.45, down_price=0.45)
    assert asyncio.run(trader._certain_win_pair(pair, live=False)) is False
