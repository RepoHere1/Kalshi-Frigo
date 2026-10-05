"""LIVE-only reaper: dead rows die, orphan holdings get liquidated.

Every test proves the guard rails: no receipt is ever invented, a fresh row
is never called phantom, DRY is never touched, and shorts are flagged rather
than covered unless the operator says so.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.jobs import live_reaper
from src.utils.database import DatabaseManager, Position


def _pos(market_id="KXORPHAN-1", side="YES", entry=0.44, qty=9, strategy="immediate_portfolio_optimization", age_h=48.0, mode="live"):
    return Position(
        market_id=market_id,
        side=side,
        entry_price=entry,
        quantity=qty,
        strategy=strategy,
        mode=mode,
        timestamp=datetime.now(timezone.utc) - timedelta(hours=age_h),
    )


class _FakeClient:
    def __init__(self, holdings, fills=None, market=None):
        self._holdings = dict(holdings)
        self._fills = fills or {}
        self._market = market or {
            "status": "open",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.42",
            "no_bid_dollars": "0.58",
            "no_ask_dollars": "0.60",
        }
        self.placed = []
        self.cancelled = []

    async def get_positions(self, ticker=None):
        if ticker:
            return {"market_positions": [{"ticker": ticker, "position_fp": str(self._holdings.get(ticker, 0.0))}]}
        return {
            "market_positions": [
                {"ticker": t, "position_fp": str(q)}
                for t, q in self._holdings.items()
            ]
        }

    async def get_fills(self, ticker=None, limit=100, cursor=None):
        return {"fills": list(self._fills.get(ticker) or [])}

    async def get_market(self, ticker):
        return {"market": self._market}

    async def get_balance(self):
        return {"balance": 100000}

    async def place_order(self, **kw):
        self.placed.append(kw)
        return {"order": {"order_id": "oid-1", "status": "resting"}}


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(db_path=str(tmp_path / "reaper.db"))
    asyncio.run(manager.initialize())
    return manager


def _seed(db, *rows):
    async def _go():
        await db.initialize()
        for r in rows:
            r.id = await db.add_position(r)
    asyncio.run(_go())


def _open_live(db):
    return asyncio.run(db.get_open_live_positions(mode="live"))


def _trade_logs(db):
    async def _go():
        return await db.get_all_trade_logs()
    return asyncio.run(_go())


def test_phantom_row_with_no_kalshi_holding_closes_at_zero(db):
    row = _pos("KXGONE-1")
    _seed(db, row)
    client = _FakeClient({}, fills={})
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert summary["phantom_closed"] == 1
    assert summary["phantom_tickers"] == ["KXGONE-1"]
    assert _open_live(db) == []
    logs = _trade_logs(db)
    assert len(logs) == 1
    assert logs[0].exit_reason == "no_kalshi_position"
    assert logs[0].pnl == pytest.approx(0.0)
    assert logs[0].mode == "live"


def test_a_fresh_row_is_never_called_phantom(db):
    _seed(db, _pos("KXNEW-1", age_h=0.0))
    client = _FakeClient({})
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert summary["phantom_closed"] == 0
    assert len(_open_live(db)) == 1


def test_a_row_kalshi_still_holds_is_never_closed(db):
    _seed(db, _pos("KXHELD-1"))
    client = _FakeClient({"KXHELD-1": 9.0})
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert summary["phantom_closed"] == 0
    assert summary["protected"] == ["KXHELD-1"]
    assert summary["liquidated"] == []
    assert len(_open_live(db)) == 1


def test_flat_kalshi_rows_are_counted_but_never_touched(db):
    _seed(db, _pos("KXGONE-1"))
    client = _FakeClient(
        {"KXFLAT-1": 0.0, "KXFLAT-2": 0.0, "KXFLAT-3": 0.0}
    )
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert summary["flat_rows_on_kalshi"] == 3
    assert summary["liquidated"] == []
    assert summary["shorts_flagged"] == []


def test_orphan_holding_is_sold_at_the_bid(db):
    client = _FakeClient(
        {"KXORPHAN-1": 9.0},
        fills={
            "KXORPHAN-1": [
                {"ticker": "KXORPHAN-1", "side": "yes", "action": "buy", "created_time": "2026-10-04T10:00:00Z"}
            ]
        },
    )
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert len(summary["liquidated"]) == 1
    sold = summary["liquidated"][0]
    assert sold["ticker"] == "KXORPHAN-1"
    assert sold["side"] == "yes"
    assert sold["contracts"] == 9
    assert sold["price"] == pytest.approx(0.40, abs=0.005)
    assert client.placed and client.placed[0]["action"] == "sell"
    assert client.placed[0]["count"] == 9


def test_short_is_flagged_not_covered_by_default(db):
    client = _FakeClient(
        {"KXDJIA-SHORT": -4.0},
        fills={
            "KXDJIA-SHORT": [
                {"ticker": "KXDJIA-SHORT", "side": "no", "action": "sell", "created_time": "2026-10-04T10:00:00Z"}
            ]
        },
    )
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert summary["liquidated"] == []
    assert len(summary["shorts_flagged"]) == 1
    assert summary["shorts_flagged"][0]["ticker"] == "KXDJIA-SHORT"
    assert client.placed == [], "covering a short spends real cash and is an operator call"


def test_short_is_covered_when_the_operator_opts_in(db):
    client = _FakeClient(
        {"KXDJIA-SHORT": -4.0},
        fills={
            "KXDJIA-SHORT": [
                {"ticker": "KXDJIA-SHORT", "side": "no", "action": "sell", "created_time": "2026-10-04T10:00:00Z"}
            ]
        },
    )
    summary = asyncio.run(live_reaper.reap_live_book(db, client, allow_shorts=True))
    assert summary["shorts_flagged"] == []
    assert len(summary["liquidated"]) == 1
    assert client.placed[0]["action"] == "buy"
    assert client.placed[0]["count"] == 4


def test_unknown_side_is_left_alone(db):
    client = _FakeClient({"KXMYSTERY-1": 5.0}, fills={})
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert summary["liquidated"] == []
    assert any("KXMYSTERY-1" in e for e in summary["errors"])
    assert client.placed == []


def test_liquidation_is_bounded_per_pass(db):
    holdings = {f"KXBULK-{i}": 3.0 for i in range(8)}
    fills = {
        t: [{"ticker": t, "side": "yes", "action": "buy", "created_time": "2026-10-04T10:00:00Z"}]
        for t in holdings
    }
    client = _FakeClient(holdings, fills=fills)
    summary = asyncio.run(live_reaper.reap_live_book(db, client, max_liquidations=2))
    assert len(summary["liquidated"]) == 2


def test_dry_rows_are_never_reaped(db):
    async def _go():
        await db.initialize()
        row = _pos("KXDRY-1")
        row.mode = "dry"
        row.id = await db.add_position(row)
        return row
    row = asyncio.run(_go())
    client = _FakeClient({})
    summary = asyncio.run(live_reaper.reap_live_book(db, client))
    assert summary["phantom_closed"] == 0
    assert summary["rows_checked"] == 0, "get_open_live_positions must not see DRY rows"
    assert asyncio.run(db.get_open_positions(mode="dry"))


def test_reaper_interval_gate():
    reaper = live_reaper.LiveReaper(db_manager=None, client=None, interval_sec=900.0)
    assert reaper.due(0.0) is True
    reaper.maybe_run  # attribute exists
    reaper._next_at = 10_000.0
    assert reaper.due(1.0) is False