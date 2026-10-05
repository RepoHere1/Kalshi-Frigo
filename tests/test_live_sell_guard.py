"""A LIVE sell must be backed by a real holding and must actually fill.

Two bugs this locks down, both found on the live account:

1. Kalshi accepts a sell of contracts the account does not hold and turns it
   into a negative position. A local row with nothing behind it (a phantom)
   therefore could create a real short.
2. A resting limit sell returned True, and callers credit P&L and close the
   row on a True return. So a maker sell that never filled still "closed" the
   position locally - which is how a real holding ended up with no local row
   tracking it at all.
"""

import asyncio
import logging

import pytest

from src.jobs.execute import _confirm_live_sell, _live_holding_yes, place_sell_limit_order
from src.utils.database import Position


def _pos(ticker="KXBTC15M-26OCT050430-30", side="NO", qty=9, entry=0.69):
    return Position(
        market_id=ticker,
        side=side,
        entry_price=entry,
        quantity=qty,
        timestamp=None,
    )


class _Client:
    def __init__(self, holdings=None, hold_values=None):
        # final holding
        self.holding = holdings
        # values handed back in order, for the polling tests
        self.sequence = list(hold_values or [])
        self.cancelled = []
        self.orders = []

    async def get_positions(self, ticker=None):
        if self.sequence:
            self.holding = self.sequence.pop(0)
        return {"market_positions": [{"ticker": ticker, "position_fp": str(self.holding)}]}

    async def get_market(self, ticker):
        return {
            "market": {
                "status": "open",
                "yes_bid_dollars": "0.30",
                "yes_ask_dollars": "0.31",
                "no_bid_dollars": "0.68",
                "no_ask_dollars": "0.70",
            }
        }

    async def get_balance(self):
        return {"balance": 500000}

    async def place_order(self, **kw):
        self.orders.append(kw)
        return {"order": {"order_id": "oid-sell", "status": "resting"}}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}


class _DB:
    async def get_open_positions(self, mode=None):
        return [_pos()]

    async def get_open_live_positions(self, mode=None):
        return [_pos()]


def test_holding_read_is_yes_terms():
    client = _Client(holdings=9.40)
    assert asyncio.run(_live_holding_yes(client, "KX-1")) == 9.40
    client = _Client(holdings=-9.47)
    assert asyncio.run(_live_holding_yes(client, "KX-1")) == -9.47


def test_sell_confirmation_waits_for_the_holding_to_shrink():
    client = _Client(hold_values=[9.0, 0.0])
    ok = asyncio.run(
        _confirm_live_sell(client, _pos(), before=-9.0, wait_seconds=0.05, logger=logging.getLogger("t"))
    )
    assert ok is True


def test_sell_confirmation_is_false_when_the_holding_never_moves():
    client = _Client(hold_values=[-9.0, -9.0, -9.0])
    ok = asyncio.run(
        _confirm_live_sell(client, _pos(), before=-9.0, wait_seconds=0.05, logger=logging.getLogger("t"))
    )
    assert ok is False


def test_unreadable_holding_is_never_treated_as_a_fill():
    class _Broken(_Client):
        async def get_positions(self, ticker=None):
            raise RuntimeError("api down")

    ok = asyncio.run(
        _confirm_live_sell(_Broken(), _pos(), before=-9.0, wait_seconds=0.05, logger=logging.getLogger("t"))
    )
    assert ok is False


def test_live_sell_is_refused_when_kalshi_holds_nothing(monkeypatch):
    """The phantom-row case: a sell here would open a real short."""
    from src.jobs import broker as broker_mod

    monkeypatch.setattr(
        broker_mod, "broker_for_mode", lambda mode, **kw: _StubBroker()
    )
    client = _Client(holdings=0.0)
    ok = asyncio.run(
        place_sell_limit_order(_pos(), 0.68, _DB(), client, live_mode=True)
    )
    assert ok is False
    assert client.orders == [], "no order may reach Kalshi without a holding"


def test_live_sell_is_refused_when_the_holding_is_short_of_the_row(monkeypatch):
    from src.jobs import broker as broker_mod

    monkeypatch.setattr(
        broker_mod, "broker_for_mode", lambda mode, **kw: _StubBroker()
    )
    client = _Client(holdings=-4.0)  # holds 4, row claims 9
    ok = asyncio.run(
        place_sell_limit_order(_pos(), 0.68, _DB(), client, live_mode=True)
    )
    assert ok is False
    assert client.orders == []


def test_live_sell_that_never_fills_returns_false_and_cancels(monkeypatch):
    from src.jobs import broker as broker_mod

    monkeypatch.setattr(
        broker_mod, "broker_for_mode", lambda mode, **kw: _StubBroker()
    )
    client = _Client(holdings=-9.0)
    ok = asyncio.run(
        place_sell_limit_order(_pos(), 0.68, _DB(), client, live_mode=True)
    )
    assert ok is False, "an unfilled maker sell must not read as a completed exit"
    assert client.orders, "the order was placed, then cancelled"
    assert client.cancelled == ["oid-sell"]


def test_live_sell_that_fills_returns_true(monkeypatch):
    from src.jobs import broker as broker_mod

    monkeypatch.setattr(
        broker_mod, "broker_for_mode", lambda mode, **kw: _StubBroker()
    )
    # Before the sell: 9 NO held. After: flat.
    client = _Client(holdings=-9.0, hold_values=[-9.0, 0.0])
    ok = asyncio.run(
        place_sell_limit_order(_pos(), 0.68, _DB(), client, live_mode=True)
    )
    assert ok is True
    assert client.cancelled == []


class _StubBroker:
    simulated = False

    async def available_cents(self):
        return 500000

    async def submit(self, req):
        return {"order": {"order_id": "oid-sell", "status": "resting"}}


def test_dry_sell_never_reads_the_holding(monkeypatch):
    """DRY keeps its historic behaviour: place, report success, move on."""
    from src.jobs import broker as broker_mod

    class _DryBroker(_StubBroker):
        async def submit(self, req):
            return {"order": {"order_id": "dry-1", "status": "filled", "simulated": True}}

    monkeypatch.setattr(
        broker_mod, "broker_for_mode", lambda mode, **kw: _DryBroker()
    )
    reads = []

    class _Counting(_Client):
        async def get_positions(self, ticker=None):
            reads.append(ticker)
            return await super().get_positions(ticker)

    client = _Counting(holdings=0.0)
    ok = asyncio.run(
        place_sell_limit_order(_pos(), 0.68, _DB(), client, live_mode=False)
    )
    assert ok is True
    assert reads == [], "DRY must not consult Kalshi holdings"