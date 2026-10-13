"""Wiring tests: pair legs, the risk-only context contract, and the pair gates."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.jobs import certain_win as cw
from src.jobs import market_context as mc
from src.jobs.ladder_trader import UpDownConfig, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket


def test_pair_legs_never_exceed_the_one_dollar_order_cap():
    assert cw.contracts_within_leg_cap(0.45, 0.45, 24.75) == 2  # 2 x 0.45 <= 1.00
    assert cw.contracts_within_leg_cap(0.30, 0.60, 24.75) == 1  # 0.60 leg caps at 1
    assert cw.contracts_within_leg_cap(0.05, 0.90, 100.0) == 1  # 0.90 leg caps at 1
    assert cw.contracts_within_leg_cap(0.45, 0.45, 0.50) == 0  # budget buys no pair


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
    def __init__(self, edge=0.0, size=1.0, tier=None, calendar="fresh", text=""):
        self.value = mc.ContextState(True, edge, size, tier, calendar, text)

    def state(self, *_args, **_kwargs):
        return self.value


def _trader(ctx):
    trader = UpDownTrader(_spot(), Btc15mFeed(series="KXBTC15M"), UpDownConfig())
    trader._context = ctx
    return trader


def _pair():
    return SimpleNamespace(ticker="KXBTC15M-26OCT1300-T85000", up_price=0.45, down_price=0.45)


def test_context_never_moves_fair_value():
    base = _trader(_Ctx()).evaluate(_market(), live=False)
    tilted = _trader(_Ctx(edge=0.01, text=" | context: test"))
    signal = tilted.evaluate(_market(), live=False)
    assert signal.fair == pytest.approx(base.fair, abs=1e-9)
    assert "context: test" in signal.reason


def test_context_edge_bar_can_block_a_base_trade():
    base = _trader(_Ctx()).evaluate(_market(), live=False)
    assert base.actionable, "precondition: the base scorer takes this market"
    blocked = _trader(_Ctx(edge=0.5)).evaluate(_market(), live=False)
    assert not blocked.actionable


def test_context_size_trim_never_increases_size():
    base = _trader(_Ctx()).evaluate(_market(), live=False)
    trimmed = _trader(_Ctx(size=0.5)).evaluate(_market(), live=False)
    assert trimmed.contracts <= base.contracts


def test_high_impact_window_blocks_a_new_certain_win_pair():
    trader = _trader(_Ctx(tier="high"))
    assert asyncio.run(trader._certain_win_pair(_pair(), live=False)) is False
    assert trader.db_manager is None  # the gate fired before any order work


def test_unavailable_calendar_blocks_new_certain_win_pairs():
    trader = _trader(_Ctx(calendar="unavailable"))
    assert asyncio.run(trader._certain_win_pair(_pair(), live=False)) is False
    assert trader.db_manager is None
