"""The DRY ledger and the stored balance must move together.

`record_fill` is a read-modify-write: read the balance, subtract or add, write
the balance back, and insert the ledger row. Under WAL, two processes can each
begin a deferred transaction, both read the *same* balance, both compute a new
one, and both insert their ledger row successfully - but only the last balance
write survives. Both fills are recorded, one cash movement is lost, and the
book's own arithmetic stops holding.

The symptom is precise and was visible on the live deployment: the newest ledger
row's `cash_after` read $325.40 while the account endpoint reported $319.58,
and the running total of the ledger did not reconcile from its starting balance.

`BEGIN IMMEDIATE` takes the write lock before the read, which is the only thing
that can make the two writes atomic - they are logically one transaction.
"""
import asyncio

import pytest

from src.utils.database import connect
from src.utils.mode import ModeError, TradingMode


async def _mgr(tmp_path, name="t.db"):
    mgr = TradingMode(db_path=str(tmp_path / name))
    await mgr.dry_account()  # opens and migrates the schema
    return mgr


async def test_concurrent_fills_do_not_lose_a_cash_movement(tmp_path):
    mgr = await _mgr(tmp_path)

    async def buy(n: int):
        try:
            return await mgr.record_fill(
                market_id="KXTEST-26",
                side="NO",
                action="buy",
                quantity=1,
                price=1.0,
                note=f"c{n}",
            )
        except ModeError:
            return None

    await asyncio.gather(*(buy(i) for i in range(8)))

    ledger = await mgr.ledger(limit=1000)
    buys = [r for r in ledger if r.get("action") == "buy"]
    account = await mgr.dry_account()

    accepted = len(buys)
    spent = round(accepted * 1.0, 2)
    assert round(account["cash"], 2) == pytest.approx(300.0 - spent, abs=0.01), (
        f"{accepted} ledger rows recorded but cash moved by "
        f"{300.0 - account['cash']:.2f} - a lost update"
    )


async def test_the_newest_ledger_row_always_matches_the_balance(tmp_path):
    """cash_after is written in the same transaction as the balance."""
    mgr = await _mgr(tmp_path)
    for i in range(5):
        await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=1, price=1.0)
        account = await mgr.dry_account()
        newest = (await mgr.ledger(limit=1))[0]
        assert float(newest["cash_after"]) == pytest.approx(account["cash"], abs=0.01)


async def test_the_balance_never_follows_a_lost_update(tmp_path):
    mgr = await _mgr(tmp_path)

    async def mixed(n: int):
        try:
            return await mgr.record_fill(
                market_id="KXTEST-26",
                side="NO",
                action="buy" if n % 2 else "sell",
                quantity=1,
                price=0.5,
            )
        except ModeError:
            return None

    await asyncio.gather(*(mixed(i) for i in range(10)))

    ledger = await mgr.ledger(limit=1000)
    debits = sum(float(r["amount"]) for r in ledger if r.get("action") == "buy")
    credits = sum(float(r["amount"]) for r in ledger if r.get("action") == "sell")
    account = await mgr.dry_account()

    assert round(account["cash"], 2) == pytest.approx(300.0 - debits + credits, abs=0.01)
    assert account["cash"] >= 0.0


async def test_a_refused_fill_still_records_no_movement(tmp_path):
    """Insufficient funds must leave both the balance and the ledger untouched."""
    mgr = await _mgr(tmp_path)
    before = (await mgr.dry_account())["cash"]

    with pytest.raises(ModeError):
        await mgr.record_fill(
            market_id="KXTEST-26", side="NO", action="buy", quantity=10_000, price=1.0
        )

    after = (await mgr.dry_account())["cash"]
    assert round(after, 2) == pytest.approx(before, abs=0.01)
    assert [r for r in await mgr.ledger(limit=10) if r.get("action") == "buy"] == []


async def test_the_audit_detects_a_self_inconsistent_ledger(tmp_path):
    """A lost update must be reported, not silently tolerated."""
    mgr = await _mgr(tmp_path, "audit.db")
    await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=1, price=1.0)

    # Corrupt the running balance behind the ledger's back, as a lost update would.
    async with connect(mgr.db_path) as conn:
        await conn.execute("UPDATE runtime_config SET value = '111.11' WHERE key = 'dry_cash'")
        await conn.commit()

    report = await mgr.repair_dry_book()
    assert report["ledger_self_consistent"] is False
    assert report["ok"] is False


async def test_a_healthy_book_audits_consistent(tmp_path):
    import os

    mgr = await _mgr(tmp_path, "healthy.db")
    os.environ["DB_PATH"] = mgr.db_path
    try:
        await mgr.record_fill(market_id="KXTEST-26", side="NO", action="buy", quantity=1, price=1.0)
        report = await mgr.repair_dry_book()
        assert report["ledger_self_consistent"] is True
        assert report["ok"] is True
    finally:
        os.environ.pop("DB_PATH", None)


async def test_wal_is_enabled_when_the_database_is_initialised(tmp_path):
    """WAL comes from DatabaseManager.initialize, which every strategy calls.

    Asserted here rather than on TradingMode's connection because that is where
    it is set; test_sqlite_concurrency.py pins it directly.
    """
    from src.utils.database import DatabaseManager

    db = DatabaseManager(db_path=str(tmp_path / "shared.db"))
    await db.initialize()

    async with connect(db.db_path) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
    assert mode.lower() == "wal"
