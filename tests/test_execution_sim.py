"""DRY's execution reality: latency, slippage, partials, empty books.

The fake fill used to pay the decision-time ask instantly and in full. LIVE
pays what the book charges AFTER the round trip. These tests prove the
simulator reads its cruelty off the same live production data LIVE would hit,
and that the row, the ledger anchor and the fee all carry the real price.
"""
import time

import pytest

from src.jobs.execution_sim import SimFill, simulate_taker_fill
from src.utils.database import DatabaseManager, Position
from datetime import datetime


class _FakeBook:
    """A Kalshi client whose market and orderbook answer from dicts.

    `orderbook` is the INNER book ({"yes": [...], "no": [...]}) exactly as
    Kalshi's payload carries it inside the "orderbook" envelope; the fake
    adds the envelope the way the real client does.
    """

    def __init__(self, market, orderbook):
        self._market = market
        self._orderbook = orderbook

    async def get_market(self, ticker):
        return {"market": self._market}

    async def get_orderbook(self, ticker, depth=100):
        return {"orderbook": self._orderbook}


def _market_dollars(yes_ask):
    return {
        "yes_bid_dollars": round(yes_ask - 0.01, 2),
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": round(1.0 - yes_ask - 0.01, 2),
        "no_ask_dollars": round(1.0 - yes_ask, 2),
    }


async def test_full_fill_at_the_touch_when_the_book_is_deep():
    client = _FakeBook(
        _market_dollars(0.41), {"orderbook": {"yes": [["41", "500"]]}}
    )
    sim = await simulate_taker_fill(
        client, "T", "YES", 10, 0.41, latency_ms=0
    )
    assert sim.filled
    assert sim.quantity == 10
    assert sim.price == pytest.approx(0.41)


async def test_walk_prices_the_weighted_average_across_levels():
    client = _FakeBook(
        _market_dollars(0.41),
        {"orderbook": {"yes": [["41", "4"], ["42", "10"], ["43", "50"]]}},
    )
    sim = await simulate_taker_fill(client, "T", "YES", 10, 0.41, latency_ms=0)
    assert sim.filled
    assert sim.quantity == 10
    # 4 @ 0.41 + 6 @ 0.42 -> 0.4160
    assert sim.price == pytest.approx(0.416)


async def test_thin_book_fills_partially():
    client = _FakeBook(
        _market_dollars(0.41), {"orderbook": {"yes": [["41", "3"]]}}
    )
    sim = await simulate_taker_fill(client, "T", "YES", 10, 0.41, latency_ms=0)
    assert sim.filled
    assert sim.quantity == 3
    assert "too thin" not in sim.reason


async def test_depth_beyond_the_cap_is_never_swept():
    client = _FakeBook(
        _market_dollars(0.41), {"orderbook": {"yes": [["41", "2"], ["47", "500"]]}}
    )
    sim = await simulate_taker_fill(client, "T", "YES", 10, 0.41, latency_ms=0)
    assert sim.filled
    assert sim.quantity == 2


async def test_slipped_ask_refuses_inside_the_cap():
    """Spot held, the ask ran 6c: a capped taker order is pulled, not chased."""
    client = _FakeBook(
        _market_dollars(0.47), {"orderbook": {"yes": [["47", "500"]]}}
    )
    sim = await simulate_taker_fill(
        client, "T", "YES", 10, 0.41, max_slippage=0.03, latency_ms=0
    )
    assert not sim.filled
    assert "slipped past cap" in sim.reason


async def test_empty_book_is_an_unfilled_order():
    client = _FakeBook(
        {"yes_bid_dollars": 0, "yes_ask_dollars": 0,
         "no_bid_dollars": 0, "no_ask_dollars": 0},
        {"orderbook": {}},
    )
    sim = await simulate_taker_fill(client, "T", "YES", 10, 0.41, latency_ms=0)
    assert not sim.filled
    assert "no liquidity" in sim.reason


async def test_no_side_walks_the_no_book():
    client = _FakeBook(
        _market_dollars(0.41),
        {"orderbook": {"yes": [["41", "1"]], "no": [["59", "20"]]}},
    )
    sim = await simulate_taker_fill(client, "T", "NO", 10, 0.59, latency_ms=0)
    assert sim.filled
    assert sim.quantity == 10
    assert sim.price == pytest.approx(0.59)


async def test_zero_latency_is_allowed():
    client = _FakeBook(
        _market_dollars(0.41), {"orderbook": {"yes": [["41", "500"]]}}
    )
    started = time.monotonic()
    sim = await simulate_taker_fill(client, "T", "YES", 5, 0.41, latency_ms=0)
    assert sim.filled
    assert time.monotonic() - started < 0.2


# ---------------------------------------------------------------------------
# The row carries the real fill
# ---------------------------------------------------------------------------
async def test_update_position_fill_writes_price_and_quantity(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()
    position = Position(
        market_id="KXBTC15M-26OCT011715-15",
        side="YES",
        entry_price=0.41,
        quantity=10,
        timestamp=datetime.now(),
        rationale="sim",
        confidence=0.5,
        live=False,
        strategy="btc_updown",
        mode="dry",
    )
    pid = await db.add_position(position, allow_duplicate=True)
    await db.update_position_fill(pid, 0.416, 6)
    rows = await db.get_open_positions(mode="dry")
    row = next(r for r in rows if r.id == pid)
    assert row.entry_price == pytest.approx(0.416)
    assert row.quantity == 6


async def test_execute_position_uses_the_simulated_price(tmp_path, monkeypatch):
    """End to end: the ledger anchor, the row and the fee carry the walk."""
    from src.jobs.execute import execute_position

    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()
    position = Position(
        market_id="KXBTC15M-26OCT011715-15",
        side="YES",
        entry_price=0.41,
        quantity=10,
        timestamp=datetime.now(),
        rationale="sim",
        confidence=0.5,
        live=False,
        strategy="btc_updown",
        mode="dry",
    )
    pid = await db.add_position(position, allow_duplicate=True)
    position.id = pid

    submitted = []

    class _FakeBroker:
        async def available_cents(self):
            return 10_000

        async def submit(self, req):
            submitted.append(req)
            return {"order": {"order_id": req.client_order_id}, "simulated": True}

    monkeypatch.setattr(
        "src.jobs.execute.broker_for_mode", lambda *a, **k: _FakeBroker()
    )
    monkeypatch.setenv("DRY_LATENCY_MS", "0")

    # Book: 6 deep at 0.42, then 100 at 0.43 - inside the 3c cap, so a taker
    # sweeps both levels: full 10 at the weighted average.
    client = _FakeBook(
        _market_dollars(0.42), {"orderbook": {"yes": [["42", "6"], ["43", "100"]]}}
    )
    assert await execute_position(position, False, db, client) is True
    assert len(submitted) == 1
    req = submitted[0]
    assert req.count == 10
    assert req.fill_price == pytest.approx(0.424)
    rows = await db.get_open_positions(mode="dry")
    row = next(r for r in rows if r.id == pid)
    assert row.entry_price == pytest.approx(0.424)
    assert row.quantity == 10


async def test_execute_position_drops_the_row_when_the_book_gone(tmp_path, monkeypatch):
    from src.jobs.execute import execute_position

    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()
    position = Position(
        market_id="KXBTC15M-26OCT011715-15",
        side="YES",
        entry_price=0.41,
        quantity=10,
        timestamp=datetime.now(),
        rationale="sim",
        confidence=0.5,
        live=False,
        strategy="btc_updown",
        mode="dry",
    )
    pid = await db.add_position(position, allow_duplicate=True)
    position.id = pid

    class _FakeBroker:
        async def available_cents(self):
            return 10_000

        async def submit(self, req):
            raise AssertionError("must not reach the broker on a refused fill")

    monkeypatch.setattr(
        "src.jobs.execute.broker_for_mode", lambda *a, **k: _FakeBroker()
    )
    monkeypatch.setenv("DRY_LATENCY_MS", "0")

    # Book vanished at execution: no quote, no fill, nothing submitted. The
    # CALLER (the trader) owns dropping the now-pointless row.
    client = _FakeBook(
        {"yes_bid_dollars": 0, "yes_ask_dollars": 0,
         "no_bid_dollars": 0, "no_ask_dollars": 0},
        {"orderbook": {}},
    )
    assert await execute_position(position, False, db, client) is False
