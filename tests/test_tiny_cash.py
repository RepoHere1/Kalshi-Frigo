"""Tiny-account competence: $10.04 is LIVE now - every clip is life or death.

Kalshi's $1.00 order minimum, its ceil-to-the-cent fee, and the 20%-of-balance
clip all interact at this scale. These tests pin the arithmetic that keeps a
tiny account trading on every edge instead of stalling or overpaying.
"""
import math
import time

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


# ---------------------------------------------------------------------------
# Maker-first bar: fee-free, bid-priced
# ---------------------------------------------------------------------------
def _mkt(**kw):
    import time as _t

    from src.jobs.market_data import UpDownMarket

    return UpDownMarket(
        ticker=kw.pop("ticker", "KXBTC15M-26OCT011715-15"),
        event_ticker="KXBTC15M-26OCT011715",
        bucket=kw.pop("bucket", "26OCT011715"),
        horizon=15,
        title="BTC up in 15?",
        target=kw.pop("target", 84609.34),
        close_ts=_t.time() + kw.pop("seconds_left", 600),
        **kw,
    )


def test_maker_bar_takes_what_the_taker_bar_refuses(monkeypatch):
    """Same book, same fair value: resting at the bid clears where paying the
    ask plus the taker fee does not. That difference is real money on $10."""
    import src.jobs.ladder_trader as lt

    monkeypatch.setattr(
        lt, "fair_up_probability", lambda s, t, n, sl=900.0, *a, **k: 0.52
    )
    trader = _trader()

    # Maker window (600s > 120s patience): bid 0.44, edge 0.08 >= 0.06 bar.
    mkt = _mkt(
        seconds_left=600, yes_bid=0.44, yes_ask=0.45, no_bid=0.55, no_ask=0.56
    )
    signal = trader.evaluate(mkt, live=True)
    assert signal is not None and signal.side == "up"
    assert "maker-bar" in signal.reason

    # Taker window (60s): ask 0.45, edge 0.07 < 0.06 + 0.0385 fee -> refused.
    trader2 = _trader()
    mkt2 = _mkt(
        seconds_left=60, yes_bid=0.44, yes_ask=0.45, no_bid=0.55, no_ask=0.56
    )
    signal2 = trader2.evaluate(mkt2, live=True)
    assert signal2 is not None and signal2.side == ""
    assert "taker-bar" in signal2.reason


def test_vol_rich_never_raises_the_bar_only_annotates():
    """Momentum makes implied sigma look rich; the diffusion fair value already
    prices that move. Penalizing it twice blocked every trending window - the
    bar may only move DOWN (cheap-vol credit), never up."""
    trader = _trader()
    t0 = time.monotonic()
    for i in range(60):
        trader.rv.observe(
            84000.0 + 0.1 * i + (1.0 if i % 2 else -1.0), now=t0 - 300.0 + i * 5.0
        )
    # Far-from-target quote -> implied sigma enormous vs realized.
    mkt = _mkt(
        seconds_left=300, yes_bid=0.20, yes_ask=0.21, no_bid=0.79, no_ask=0.80
    )
    trader.evaluate(mkt, live=False)
    assert trader._vol_adj <= 0.0
