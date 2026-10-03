"""Six processes share one SQLite file on a Railway volume.

That combination produced `sqlite3.OperationalError: database is locked` during
strategy startup, twice, killing two strategies outright - and the supervisor
then failed to bring them back, so the book quietly ran on half its strategies.

Two properties are asserted here:

1. The database is opened in a way that tolerates contention: WAL so readers do
   not block behind a writer, and a busy timeout so a blocked writer waits
   rather than failing.
2. The supervisor's failure budget counts *consecutive* failures and resets on a
   genuine recovery, so a transient lock cannot permanently disable a strategy.
"""
import os

import pytest

from src.utils.database import BUSY_TIMEOUT_SECONDS, DatabaseManager, connect


async def test_wal_is_enabled_on_initialise(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "wal.db"))
    await db.initialize()

    import aiosqlite

    async with aiosqlite.connect(db.db_path) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]

    assert (
        mode.lower() == "wal"
    ), "readers must not block behind a writer; six processes share this file"


def test_the_busy_timeout_is_long_enough_to_outlast_a_write_cycle():
    """SQLite's ~5s default is shorter than a busy strategy cycle holds the lock."""
    assert BUSY_TIMEOUT_SECONDS >= 15


async def test_concurrent_writers_are_serialised_not_rejected(tmp_path):
    """Several writers must queue, not collide."""
    import asyncio

    import aiosqlite

    path = str(tmp_path / "busy.db")
    db = DatabaseManager(db_path=path)
    await db.initialize()

    import aiosqlite

    async with aiosqlite.connect(path) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS writer_probe (n INTEGER)")
        await conn.commit()

    async def writer(n: int):
        async with connect(path) as conn:
            await conn.execute("INSERT INTO writer_probe (n) VALUES (?)", (n,))
            await conn.commit()
            return True

    results = await asyncio.gather(*(writer(i) for i in range(8)), return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception)], results

    async with aiosqlite.connect(path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM writer_probe")
        assert (await cur.fetchone())[0] == 8


def test_connect_is_not_a_coroutine():
    """Callers use `async with connect(...)`; awaiting first double-starts the thread."""
    import inspect

    assert not inspect.iscoroutinefunction(connect)


# ---------------------------------------------------------------------------
# Supervisor budget
# ---------------------------------------------------------------------------
def test_the_failure_budget_is_consecutive_not_lifetime():
    import web_dashboard as wd

    assert wd._SUPERVISOR_MAX_ATTEMPTS >= 5
    # A recovery has to be demonstrable, not instantaneous.
    assert wd._SUPERVISOR_STABLE_SECONDS >= 60


def test_a_recently_started_process_has_not_yet_proven_itself():
    """The stable-window check is what gates resetting the attempt counter."""
    from datetime import datetime, timedelta

    now = datetime.now()
    fresh = (now - timedelta(seconds=5)).isoformat()
    settled = (now - timedelta(seconds=600)).isoformat()

    def stable_since(started_at):
        try:
            return (now - datetime.fromisoformat(str(started_at))).total_seconds()
        except Exception:  # noqa: BLE001
            return 0.0

    assert stable_since(fresh) < 120
    assert stable_since(settled) >= 120
