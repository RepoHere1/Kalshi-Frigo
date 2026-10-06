"""Tiny-account competence: $10.04 is LIVE now - every clip is life or death.

Kalshi's $1.00 order minimum, its ceil-to-the-cent fee, and the 20%-of-balance
clip all interact at this scale. These tests pin the arithmetic that keeps a
tiny account trading on every edge instead of stalling or overpaying.
"""
import math

import pytest

from src.jobs.broker import DryBroker, OrderRequest, build_order_request, minimum_viable_quantity
from src.jobs.ladder_trader import UpDownConfig, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed
from src.utils.mode import TradingMode


def _trader():
    spot = SpotFeed()
    spot.price = 84900.0
    spot.ts = __import__("time").time()
    spot.source = "test"
    return UpDownTrader(spot, Btc15mFeed(), UpDownConfig())


def test_size_floors_with_ceil_not_round():
    """round(1/0.45)=2 -> $0.90, an order Kalshi rejects. ceil gives 3 -> $1.35."""
    trader = _trader()
    assert trader._size(0.45, clip_usd=0.60) == 3
    assert trader._size(0.58, clip_usd=0.60) == 2  # 2 @ 0.58 = $1.16


def test_size_spends_the_budget_not_the_default_clip():
    """A $10.04 account clips at 20% ($2.01), not the $5 default."""
    trader = _trader()
    assert trader._size(0.58, clip_usd=2.01) == 3  # 3 @ 0.58 = $1.74 <= 2.01
    assert trader._size(0.41, clip_usd=2.01) == 4  # 4 @ 0.41 = $1.64


def test_minimum_viable_quantity_never_drops_below_a_dollar():
    assert minimum_viable_quantity(0.45) == 3   # 3 x 0.45 = $1.35 >= 1
    assert minimum_viable_quantity(0.10) == 10  # 10 x 0.10 = $1.00
    assert minimum_viable_quantity(0.05) == 20  # 20 x 0.05 = $1.00


def test_broker_refloor_meets_the_minimum(tmp_path):
    """Whatever _size says, the broker's floor lands at or above $1 notional."""
    from src.utils.database import DatabaseManager

    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    import asyncio

    asyncio.run(db.initialize())
    req, reason = build_order_request(
        market_id="KXTEST-26",
        side="YES",
        action="buy",
        quantity=1,  # 1 @ 0.45 = $0.45 - under the floor
        market={
            "yes_bid_dollars": 0.44, "yes_ask_dollars": 0.45,
            "no_bid_dollars": 0.54, "no_ask_dollars": 0.55,
            "status": "open",
        },
        available_cents=1_000,  # $10.00 - can afford the refloor
    )
    assert req is not None
    assert req.notional >= 1.0


def test_fee_is_ceiled_to_the_cent_like_kalshi():
    """3 @ 0.58: raw 0.0511 - Kalshi charges up; the fake book must too."""
    fee = math.ceil(0.07 * 0.58 * 0.42 * 3 * 100.0) / 100.0
    assert fee == 0.06
    assert round(0.07 * 0.58 * 0.42 * 3, 2) == 0.05  # the old, undercharging way


async def test_dry_ledger_books_the_ceiled_fee(tmp_path):
    db_path = str(tmp_path / "t.db")
    from datetime import datetime

    from src.utils.database import DatabaseManager, Position

    db = DatabaseManager(db_path=db_path)
    await db.initialize()
    position = Position(
        market_id="KXTEST-26", side="YES", entry_price=0.58, quantity=3,
        timestamp=datetime.now(), rationale="fee test", confidence=0.5,
        live=False, strategy="btc_updown", mode="dry",
    )
    position.id = await db.add_position(position, allow_duplicate=True)

    broker = DryBroker(TradingMode(db_path=db_path))
    req = OrderRequest(
        ticker="KXTEST-26", client_order_id="fee1", side="yes", action="buy",
        count=3, type_="limit", notional=1.74, fill_price=0.58,
    )
    req.yes_price = 58
    resp = await broker.submit(req)
    assert not resp.get("error")
    ledger = await broker._mode.ledger(limit=1)
    # 300 - 3*0.58 - ceiled fee 0.06 = 298.20 (the round-fee era booked 298.21... no:
    # round gave 0.05 -> 298.21; Kalshi's ceil gives 0.06 -> 298.20).
    assert ledger[0]["cash_after"] == pytest.approx(298.20)


def test_balance_below_minimum_sets_a_plain_error():
    """The cycle must say WHY it is sitting out, in words a human can act on."""
    import inspect

    from src.jobs.ladder_trader import UpDownTrader as T

    src = inspect.getsource(T.cycle)
    assert "order minimum - sitting out until funded" in src
