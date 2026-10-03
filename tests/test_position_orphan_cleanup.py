"""A position row must only exist if a fill happened.

`execute_position` needs a persisted row to record a fill against, so the
position is inserted *before* the order is submitted. That ordering is what
stopped the DRY book draining itself - but it means a refused order leaves a row
behind: no fill, no ledger debit, yet the row still reads `status='open'` and its
cost basis counts as deployed capital.

On the live book that produced 23 open positions against 12 ledger entries, a
negative cash balance, and a headline reading "$251.46 deployed of $178.10
equity" - deployed exceeding equity, which is arithmetically impossible and was
the clearest signal that the books had stopped agreeing with each other.
"""
from datetime import datetime

import pytest

from src.utils.database import DatabaseManager, Position


async def _db(tmp_path, name="t.db"):
    db = DatabaseManager(db_path=str(tmp_path / name))
    await db.initialize()
    return db


def _position(**kw):
    base = dict(
        market_id="KXBTC15M-26OCT022300-00",
        side="NO",
        entry_price=0.24,
        quantity=6,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        strategy="btc_updown",
        mode="dry",
    )
    base.update(kw)
    return Position(**base)


async def test_delete_removes_an_orphan_row(tmp_path):
    db = await _db(tmp_path)
    pid = await db.add_position(_position())
    assert pid is not None
    assert len(await db.get_open_positions(mode="dry")) == 1

    assert await db.delete_position(pid) is True
    assert await db.get_open_positions(mode="dry") == []


async def test_delete_only_removes_open_rows(tmp_path):
    """A position that was genuinely closed must never be unwindable."""
    import aiosqlite

    db = await _db(tmp_path)
    pid = await db.add_position(_position())
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (pid,))
        await conn.commit()

    assert await db.delete_position(pid) is False


async def test_delete_is_idempotent(tmp_path):
    db = await _db(tmp_path)
    pid = await db.add_position(_position())
    assert await db.delete_position(pid) is True
    assert await db.delete_position(pid) is False


async def test_a_refused_clip_leaves_no_position(tmp_path, monkeypatch):
    """End to end: the order is refused, so nothing may remain behind."""
    from src.jobs.ladder_trader import (
        UpDownConfig,
        UpDownSignal,
        UpDownTrader,
    )
    from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket

    db = await _db(tmp_path, "clip.db")
    ticker = "KXBTC15M-26OCT022300-00"

    spot = SpotFeed()
    spot.price = 84900.0
    import time as _t

    spot.ts = _t.time()
    spot.source = "test"
    feed = Btc15mFeed()
    import time as _t

    feed.markets = [
        UpDownMarket(
            ticker=ticker,
            event_ticker=ticker.rsplit("-", 1)[0],
            title="BTC up or down",
            bucket="26OCT022300",
            horizon=0,
            yes_bid=0.76,
            yes_ask=0.78,
            no_bid=0.22,
            no_ask=0.24,
            target=84500.0,
            close_ts=_t.time() + 600,
        )
    ]
    trader = UpDownTrader(spot, feed, UpDownConfig())
    trader.db_manager = db
    trader._client = object()

    async def _refused(position, live_mode, db_manager, kalshi_client):
        return False  # e.g. insufficient funds / untradable price

    monkeypatch.setattr("src.jobs.execute.execute_position", _refused)

    signal = UpDownSignal(
        ticker=ticker,
        bucket="26OCT022300",
        side="down",
        target=84500.0,
        spot=84900.0,
        spot_vs_target=400.0,
        fair=0.01,
        kalshi_price=0.24,
        edge=0.23,
        ask=0.24,
        contracts=6,
        notional=1.44,
        seconds_left=600.0,
        reason="test",
    )
    filled = await trader._place(signal, live=False)

    assert filled is False
    # The whole point: no orphan row, so deployed capital equals what was spent.
    assert await db.get_open_positions(mode="dry") == []
