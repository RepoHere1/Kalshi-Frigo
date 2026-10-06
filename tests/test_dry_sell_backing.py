"""The tallies must be arithmetically honest: no phantom sells, no 30W/0L
next to 36.4%, ever.

Two root causes shipped real wrongness to the page:
  - DryBroker credited cash for sells that no open position backed (free
    money in a ledger: the DRY "realized P&L" ran +$278 while the bot's own
    trade log said -$17.89).
  - The win-rate % came from /api/status while the W/L line came from the
    server render, and losses was trusted from SQL, so the two could not
    even agree with themselves.
"""
import pytest

from src.jobs.broker import DryBroker, OrderRequest
from src.utils.database import DatabaseManager, Position
from src.utils.mode import TradingMode
from datetime import datetime


def _request(action="sell", count=10, price=0.5, side="yes", ticker="KXBTC15M-26OCT011715-15"):
    req = OrderRequest(
        ticker=ticker,
        client_order_id=f"t-{action}-{count}",
        side=side,
        action=action,
        count=count,
        type_="limit",
        notional=round(price * count, 2),
        fill_price=price,
    )
    req.yes_price = int(round(price * 100))
    return req


async def _db_with_position(tmp_path, qty=10):
    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()
    position = Position(
        market_id="KXBTC15M-26OCT011715-15",
        side="YES",
        entry_price=0.45,
        quantity=qty,
        timestamp=datetime.now(),
        rationale="backing test",
        confidence=0.5,
        live=False,
        strategy="btc_updown",
        mode="dry",
    )
    position.id = await db.add_position(position, allow_duplicate=True)
    return db


async def test_sell_without_a_position_is_refused(tmp_path):
    """The engine of the phantom-cash bug: a sell with nothing behind it."""
    db = await _db_with_position(tmp_path, qty=0)  # no position inserted
    broker = DryBroker(TradingMode(db_path=str(tmp_path / "t.db")))
    resp = await broker.submit(_request(count=10))
    assert resp.get("error")
    assert "no open DRY position backs" in resp["error"]
    ledger = await broker._mode.ledger(limit=10)
    assert ledger == [], "a refused sell must not touch the ledger"


async def test_sell_larger_than_holdings_is_clamped(tmp_path):
    """Kalshi rejects oversells; the rehearsal closes what it actually holds."""
    db = await _db_with_position(tmp_path, qty=10)
    broker = DryBroker(TradingMode(db_path=str(tmp_path / "t.db")))
    resp = await broker.submit(_request(count=25, price=0.6))
    assert not resp.get("error")
    ledger = await broker._mode.ledger(limit=5)
    assert len(ledger) == 1
    assert ledger[0]["quantity"] == 10
    assert ledger[0]["amount"] == pytest.approx(6.0)


async def test_sell_within_holdings_passes(tmp_path):
    db = await _db_with_position(tmp_path, qty=10)
    broker = DryBroker(TradingMode(db_path=str(tmp_path / "t.db")))
    resp = await broker.submit(_request(count=10, price=0.6))
    assert not resp.get("error")
    ledger = await broker._mode.ledger(limit=5)
    assert len(ledger) == 1
    assert ledger[0]["quantity"] == 10


async def test_backing_is_per_side(tmp_path):
    """Holding YES does not back a NO sell."""
    db = await _db_with_position(tmp_path, qty=10)
    broker = DryBroker(TradingMode(db_path=str(tmp_path / "t.db")))
    resp = await broker.submit(_request(count=10, side="no"))
    assert resp.get("error")


async def test_reads_only_the_dry_book(tmp_path):
    """A LIVE position row must not back a DRY sell."""
    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()
    position = Position(
        market_id="KXBTC15M-26OCT011715-15",
        side="YES",
        entry_price=0.45,
        quantity=10,
        timestamp=datetime.now(),
        rationale="live row",
        confidence=0.5,
        live=True,
        strategy="btc_updown",
        mode="live",
    )
    position.id = await db.add_position(position, allow_duplicate=True)
    broker = DryBroker(TradingMode(db_path=str(tmp_path / "t.db")))
    resp = await broker.submit(_request(count=10))
    assert resp.get("error")


# ---------------------------------------------------------------------------
# The tally math
# ---------------------------------------------------------------------------
def test_losses_are_derived_never_trusted():
    """pnl == 0 rows counted as neither win nor loss rendered 30W / 0L."""
    from web_dashboard import _row_trades

    row = {"trades": 30, "wins": 11, "losses": 0, "realized_pnl": -17.89}
    t = _row_trades([row])
    assert t["losses"] == 19
    assert t["win_rate"] == pytest.approx(36.7)


def test_wins_can_never_exceed_trades():
    from web_dashboard import _row_trades

    t = _row_trades([{"trades": 5, "wins": 30}])
    assert t["wins"] == 5
    assert t["losses"] == 0
    assert t["win_rate"] == 100.0


def test_win_rate_and_wl_share_one_source():
    """The % and the W/L line must come from the same numbers, server and JS."""
    src = open("web_dashboard.py", encoding="utf-8").read()
    # The JS re-render must also refresh the W/L note, not just the %.
    assert "tWinRateNote" in src
    assert "t.losses" in src
    # And the template carries the id so both paths write the same node.
    assert 'id="tWinRateNote"' in src
