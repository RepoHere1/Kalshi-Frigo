"""btc_updown must be able to exit, in LIVE, on its own.

The trader only ever opened positions. Closing them was someone else's job:
`run_tracking` lives inside BeastModeBot, so in DRY the ai_directional process
quietly closed btc_updown's positions for it.

Arming btc_updown alone in LIVE - the plan for a small first run - therefore had
nothing tracking it: no take-profit, no stop-loss, nothing but Kalshi's own
settlement at the boundary, and no close ever written to trade_logs.

These drive the real code path with API responses shaped like the ones Kalshi
actually returns (`*_dollars` strings; the `yes_price` fields it used to be read
from no longer exist).
"""
from datetime import datetime, timedelta

import pytest

from src.jobs.ladder_trader import UpDownConfig, UpDownSignal, UpDownTrader
from src.jobs.market_data import Btc15mFeed, SpotFeed, UpDownMarket
from src.utils.database import DatabaseManager, Position
from src.utils.market_prices import get_market_prices


def _trader(tmp_path, live=False):
    spot = SpotFeed()
    spot.price = 84_900.0
    import time as _t

    spot.ts = _t.time()
    spot.source = "test"
    feed = Btc15mFeed()
    feed.markets = [
        UpDownMarket(
            ticker="KXBTC15M-26OCT041200-15",
            event_ticker="KXBTC15M-26OCT041200",
            title="BTC up or down",
            bucket="26OCT041200",
            horizon=15,
            yes_bid=0.40,
            yes_ask=0.42,
            no_bid=0.58,
            no_ask=0.60,
            target=84_500.0,
            close_ts=_t.time() + 600,
        )
    ]
    trader = UpDownTrader(spot, feed, UpDownConfig())
    trader.db_manager = DatabaseManager(db_path=str(tmp_path / "t.db"))
    return trader


async def _ready(trader):
    await trader.db_manager.initialize()


def _signal(**kw):
    base = dict(
        ticker="KXBTC15M-26OCT041200-15",
        bucket="26OCT041200",
        side="down",
        target=84_500.0,
        spot=84_900.0,
        spot_vs_target=400.0,
        fair=0.01,
        kalshi_price=0.59,
        edge=0.58,
        ask=0.59,
        contracts=3,
        notional=3 * 0.41,
        seconds_left=600.0,
        reason="test",
    )
    base.update(kw)
    return UpDownSignal(**base)


async def test_the_trader_runs_position_tracking(tmp_path, monkeypatch):
    """The gap: nothing tracked a btc_updown position unless another strategy ran."""
    trader = _trader(tmp_path)
    await _ready(trader)

    called = []

    async def _tracking(db_manager):
        called.append(db_manager is not None)

    monkeypatch.setattr("src.jobs.track.run_tracking", _tracking)
    monkeypatch.setattr("src.jobs.broker.should_trade_live", lambda: False, raising=False)

    await trader.cycle()

    assert called == [True], "cycle() never ran position tracking"


async def test_a_tracking_failure_does_not_stop_the_trader(tmp_path, monkeypatch):
    """A tracking error must not take the entry path down with it."""
    trader = _trader(tmp_path)
    await _ready(trader)

    async def _boom(db_manager):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("src.jobs.track.run_tracking", _boom)
    monkeypatch.setattr("src.jobs.broker.should_trade_live", lambda: False, raising=False)

    summary = await trader.cycle()  # must not raise
    assert "book" in summary or "ticker" in str(summary)


@pytest.mark.live
async def test_an_open_position_is_closed_from_its_own_process(tmp_path, monkeypatch):
    """The whole point: btc_updown alone must be able to take profit.

        Requires real Kalshi credentials.
    un_tracking takes no client argument -
        it builds its own - so a LIVE sell cannot be exercised without an account,
        and the DRY path is covered by the dry tests. Skipped by default precisely
        because it must not be claimed green on the strength of a DRY run.
    """
    import os

    if not os.environ.get("KALSHI_API_KEY") or not os.environ.get("KALSHI_PRIVATE_KEY"):
        pytest.skip("needs live Kalshi credentials to exercise a real sell")
    trader = _trader(tmp_path)
    await _ready(trader)
    db = trader.db_manager

    pid = await db.add_position(
        Position(
            market_id="KXBTC15M-26OCT041200-15",
            side="NO",
            entry_price=0.41,
            quantity=3,
            timestamp=datetime.now(),
            rationale="live test",
            confidence=0.9,
            live=True,
            strategy="btc_updown",
            mode="live",
            take_profit_price=0.45,
        )
    )
    assert pid is not None

    from src.jobs.track import run_tracking

    # NO has fallen from 0.41 to 0.30, so the 0.45 take-profit is satisfied.
    quotes = {
        "ticker": "KXBTC15M-26OCT041200-15",
        "status": "open",
        "yes_bid_dollars": "0.7000",
        "yes_ask_dollars": "0.7200",
        "no_bid_dollars": "0.2800",
        "no_ask_dollars": "0.3000",
    }
    assert get_market_prices(quotes) == (0.70, 0.72, 0.28, 0.30)

    class _Client:
        async def get_market(self, ticker):
            return quotes

        async def close(self):
            return None

    exits = []

    async def _fake_sell(ticker, **kw):
        exits.append((ticker, kw.get("action")))
        return {"status": "filled"}

    import src.jobs.track as track_mod

    monkeypatch.setattr(track_mod, "place_sell_limit_order", _fake_sell, raising=False)

    import src.jobs.broker as broker_mod

    async def _live():
        return "live"

    monkeypatch.setattr(broker_mod, "current_mode", _live)
    monkeypatch.setattr("src.jobs.execute.should_trade_live", lambda: True, raising=False)
    await run_tracking(db)

    assert exits, "a LIVE position past its take-profit was not closed"
    assert exits[0][1] == "sell"

    logs = await db.get_trade_logs_for_market("KXBTC15M-26OCT041200-15")
    assert logs, "the close was never written to trade_logs"


async def test_take_profit_uses_the_v2_price_fields_not_the_removed_ones():
    """`yes_price` no longer exists; reading it priced every exit at $0.00."""
    from src.jobs.track import should_exit_position

    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(
        {
            "yes_bid_dollars": "0.7000",
            "yes_ask_dollars": "0.7200",
            "no_bid_dollars": "0.2800",
            "no_ask_dollars": "0.3000",
        }
    )
    position = Position(
        market_id="KXTEST-26",
        side="NO",
        entry_price=0.41,
        quantity=3,
        timestamp=datetime.now(),
        rationale="t",
        confidence=0.9,
        live=True,
        strategy="btc_updown",
        take_profit_price=0.45,
        mode="live",
    )
    should_exit, reason, exit_price = await should_exit_position(
        position, (yes_bid + yes_ask) / 2, (no_bid + no_ask) / 2, "open", None
    )
    assert should_exit is True
    assert reason == "take_profit"
    assert exit_price > 0.0, "a $0 exit would be refused and strand the position"
