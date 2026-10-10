"""The DRY audit must catch the three ways the book contradicts itself.

These are the exact faults that made the headline numbers disagree on the live
book: positions with no fill behind them, closes recorded more than once, and a
cash balance that cannot be derived from the ledger's own debits and credits.
"""
from datetime import datetime

import pytest

from src.utils.database import DatabaseManager, Position
from src.utils import mode as mode_mod
from src.utils.mode import MODE_DRY, TradingMode, run


async def _db(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "t.db"))
    await db.initialize()
    return db


def _position(ticker="KXTEST-26", side="NO", price=0.25, qty=10, strategy="ai_directional"):
    return Position(
        market_id=ticker,
        side=side,
        entry_price=price,
        quantity=qty,
        timestamp=datetime.now(),
        rationale="test",
        confidence=0.8,
        live=False,
        strategy=strategy,
        mode="dry",
    )


async def test_total_pnl_is_equity_minus_starting_not_a_double_count(tmp_path, monkeypatch):
    """The headline P&L must agree with the tiles beside it.

    `total_pnl` was computed as `cash - starting + realized`. But cash is the
    running ledger that was already debited on every buy and credited on every
    close, so `cash - starting` already contains the realized P&L; adding the
    trade_logs `realized` again summed every close twice. The production DRY
    page showed "realized P&L $4,261.81" beside an equity of $3,011.89 on a
    $300 start - an arithmetic contradiction inside one row of tiles.

    The only derivation that cannot contradict its own row is equity minus
    starting, so that is what total_pnl is now.
    """
    from src.utils.database import TradeLog

    db = await _db(tmp_path)
    mgr = TradingMode(db_path=db.db_path)
    monkeypatch.setenv("DB_PATH", db.db_path)

    # A buy: $10 of cash becomes a position.
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=1.0)
    await db.add_position(_position(ticker="KXTEST-26", price=1.0, qty=10))
    # A winning close: +$2.50 into the ledger cash via record_fill sell,
    # and a matching +$2.50 trade log row.
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="sell", quantity=10, price=1.25)
    await db.add_trade_log(
        TradeLog(
            market_id="KXTEST-26",
            side="NO",
            entry_price=1.0,
            exit_price=1.25,
            quantity=10,
            pnl=2.5,
            entry_timestamp=datetime.now(),
            exit_timestamp=datetime.now(),
            rationale="x",
            strategy="ai_directional",
            exit_reason="take_profit",
            mode="dry",
        )
    )

    account = await mgr.book_account(MODE_DRY)
    # cash: 300 - 10 + 12.50 = 302.50. realized (trade_logs): 2.50.
    assert account["cash"] == pytest.approx(302.50, abs=0.01)
    assert account["realized"] == pytest.approx(2.50, abs=0.01)
    # equity = cash + deployed (position row still open) - but for the identity
    # test what matters is that total_pnl is equity - starting, never
    # cash - starting + realized (which would be 5.00).
    expected_total = round(account["equity"] - account["starting_balance"], 2)
    assert account["total_pnl"] == pytest.approx(expected_total, abs=0.01)
    assert account["total_pnl"] != pytest.approx(5.00, abs=0.01)
    assert account["return_pct"] == pytest.approx(
        round(expected_total / account["starting_balance"] * 100, 2), abs=0.01
    )


async def test_a_clean_book_audits_ok(tmp_path, monkeypatch):
    mgr = TradingMode(db_path=str(tmp_path / "a.db"))
    db = await _db(tmp_path)
    monkeypatch.setenv("DB_PATH", db.db_path)

    pid = await db.add_position(_position())
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)

    report = await mgr.repair_dry_book()
    assert report["orphan_positions"] == 0
    assert report["duplicate_closes"] == 0
    assert report["cash"] == pytest.approx(297.50, abs=0.01)
    assert report["ok"] is True
    assert pid is not None


async def test_a_position_with_no_fill_is_reported_as_an_orphan(tmp_path, monkeypatch):
    """The gap that produced 23 open positions against 12 ledger debits."""
    mgr = TradingMode(db_path=str(tmp_path / "b.db"))
    db = await _db(tmp_path)
    monkeypatch.setenv("DB_PATH", db.db_path)

    await db.add_position(_position(price=0.24, qty=6, strategy="btc_updown"))

    report = await mgr.repair_dry_book()
    assert report["orphan_positions"] == 1
    assert report["orphan_notional"] == pytest.approx(1.44, abs=0.01)
    assert report["ok"] is False


async def test_negative_cash_is_reported(tmp_path, monkeypatch):
    mgr = TradingMode(db_path=str(tmp_path / "c.db"))
    db = await _db(tmp_path)
    monkeypatch.setenv("DB_PATH", db.db_path)

    # Simulate the state directly: a ledger that cannot fund itself.
    async with mgr._conn() as conn:
        await mgr._set(conn, mode_mod._CASH_KEY, "-73.36")
        await conn.commit()
    assert float((await mgr.dry_account())["cash"]) < 0

    report = await mgr.repair_dry_book()
    assert report["cash"] < 0
    assert report["ok"] is False


async def test_duplicate_closes_are_counted_and_their_pnl_surfaced(tmp_path, monkeypatch):
    """Every close was written twice, so every result was counted twice."""
    import aiosqlite

    mgr = TradingMode(db_path=str(tmp_path / "d.db"))
    db = await _db(tmp_path)
    monkeypatch.setenv("DB_PATH", db.db_path)

    from src.utils.database import TradeLog

    for _ in range(2):
        await db.add_trade_log(
            TradeLog(
                market_id="KXTEST-26",
                side="NO",
                entry_price=0.939,
                exit_price=0.0305,
                quantity=81,
                pnl=-73.59,
                entry_timestamp=datetime.now(),
                exit_timestamp=datetime(2026, 10, 3, 3, 42, 7),
                rationale="dup",
                strategy="btc_updown",
                exit_reason="take_profit",
                mode="dry",
            )
        )

    report = await mgr.repair_dry_book()
    assert report["duplicate_closes"] == 1
    assert report["duplicate_pnl"] == pytest.approx(-73.59, abs=0.01)
    assert report["ok"] is False

    async with aiosqlite.connect(db.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT COUNT(*) c, SUM(pnl) s FROM trade_logs"
            " WHERE COALESCE(NULLIF(mode,''),'dry')='dry'"
        )
        row = await cur.fetchone()
    assert row["c"] == 2
    assert row["s"] == pytest.approx(-147.18, abs=0.01)


async def test_the_audit_never_mutates_history(tmp_path, monkeypatch):
    """It reports; it does not quietly rewrite the record."""
    import aiosqlite

    mgr = TradingMode(db_path=str(tmp_path / "e.db"))
    db = await _db(tmp_path)
    monkeypatch.setenv("DB_PATH", db.db_path)
    await db.add_position(_position(strategy="btc_updown"))

    async with aiosqlite.connect(db.db_path) as conn:
        before_pos = (await (await conn.execute("SELECT COUNT(*) c FROM positions")).fetchone())[0]

    ledger_before = await mgr.ledger(limit=1000)

    await mgr.repair_dry_book()

    async with aiosqlite.connect(db.db_path) as conn:
        after_pos = (await (await conn.execute("SELECT COUNT(*) c FROM positions")).fetchone())[0]

    assert before_pos == after_pos
    assert len(ledger_before) == len(await mgr.ledger(limit=1000))


async def test_a_reset_clears_the_whole_simulated_book(tmp_path, monkeypatch):
    """A reset that only resets cash leaves an equity figure that cannot be true.

    It used to empty the ledger and leave DRY positions and closes behind,
    producing a book claiming $300 cash and $250.64 of deployed capital at the
    same time - equity of $550.64 on a $300 account.
    """
    from src.utils.database import TradeLog

    db = await _db(tmp_path)
    # Same file in production: the mode tables and the trading tables share one
    # database, which is what lets the reset clear both halves of the book.
    mgr = TradingMode(db_path=db.db_path)
    monkeypatch.setenv("DB_PATH", db.db_path)

    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=10, price=0.25)
    await db.add_position(_position(strategy="btc_updown"))
    await db.add_trade_log(
        TradeLog(
            market_id="KXTEST-26",
            side="NO",
            entry_price=0.25,
            exit_price=0.30,
            quantity=10,
            pnl=0.5,
            entry_timestamp=datetime.now(),
            exit_timestamp=datetime.now(),
            rationale="x",
            strategy="btc_updown",
            exit_reason="take_profit",
            mode="dry",
        )
    )
    assert await db.get_open_positions(mode="dry")

    account = await mgr.reset_dry_account()

    assert account["cash"] == pytest.approx(300.0, abs=0.01)
    assert account["open_positions"] == 0
    assert account["closed_trades"] == 0
    assert await db.get_open_positions(mode="dry") == []
    report = await mgr.repair_dry_book()
    assert report["orphan_positions"] == 0
    assert report["ok"] is True


async def test_a_reset_never_touches_the_live_book(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    mgr = TradingMode(db_path=db.db_path)
    monkeypatch.setenv("DB_PATH", db.db_path)

    await db.add_position(
        Position(
            market_id="KXLIVE-26",
            side="YES",
            entry_price=0.5,
            quantity=2,
            timestamp=datetime.now(),
            rationale="live",
            confidence=0.8,
            live=True,
            strategy="ai_directional",
            mode="live",
        )
    )
    await db.add_position(_position(strategy="btc_updown"))

    await mgr.reset_dry_account()

    remaining = await db.get_open_positions(mode="live")
    assert len(remaining) == 1
    assert remaining[0].market_id == "KXLIVE-26"
