"""A recorded pid must still name the same process, and intent must outlive it.

The dashboard stores each strategy's pid in the database, which lives on a
Railway volume and therefore survives a redeploy. The processes do not survive it.
A bare pid check therefore reads a stranger's pid as a live trading bot, and Stop
then signals a process that has nothing to do with trading. These tests pin the
two properties that fix that: a pid is only believed alongside the instance and
kernel start-time recorded with it, and the operator's intent to keep a strategy
running is stored separately from the pid so a crash is recoverable.
"""
import os

import pytest

import src.utils.strategy_runtime as rt


async def test_a_pid_from_a_previous_instance_is_not_a_live_strategy(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("ai_directional", os.getpid(), "paper", "cli.py run")
    row = (await store.snapshot())["ai_directional"]
    assert rt.owns_process(row) is True

    stale = dict(row, instance="some-earlier-instance")
    assert rt.owns_process(stale) is False


async def test_a_recycled_pid_is_not_mistaken_for_the_strategy(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("quick_flip", os.getpid(), "paper", "cli.py run")
    row = (await store.snapshot())["quick_flip"]

    real = rt.process_start_ticks(os.getpid())
    if real is None:
        pytest.skip("kernel start-time ticks unavailable on this platform")
    assert rt.owns_process(dict(row, start_ticks=real + 999)) is False


async def test_intent_to_run_outlives_the_process(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("market_making", os.getpid(), "paper", "cli.py run")
    assert "market_making" in await store.desired()

    await store.record_stop("market_making", "exited on its own")
    await store.set_desired("market_making", True)
    assert "market_making" in await store.desired()

    await store.set_desired("market_making", False)
    assert "market_making" not in await store.desired()


async def test_columns_are_added_to_a_table_that_predates_them(tmp_path):
    import aiosqlite

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            "CREATE TABLE strategy_runtime (name TEXT PRIMARY KEY, pid INTEGER,"
            " started_at TEXT, mode TEXT, command TEXT, stop_reason TEXT, stopped_at TEXT);"
        )
        await conn.commit()

    store = rt.StrategyRuntime(db_path=path)
    await store.record_start("beast_mode", os.getpid(), "paper", "cli.py run")
    row = (await store.snapshot())["beast_mode"]
    assert row["instance"] == rt.INSTANCE


async def test_a_stopped_strategy_is_not_live(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("safe_compounder", os.getpid(), "paper", "cli.py run")
    await store.record_stop("safe_compounder", "stopped by operator")
    row = (await store.snapshot())["safe_compounder"]
    assert rt.owns_process(row) is False
    assert row["stop_reason"] == "stopped by operator"
