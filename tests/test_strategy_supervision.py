"""A recorded pid must still name the same process, and intent must outlive it.

The dashboard stores each strategy's pid in the database, which lives on a
Railway volume and therefore survives a redeploy. The processes do not survive it.
A bare pid check therefore reads a stranger's pid as a live trading bot, and Stop
then signals a process that has nothing to do with trading. These tests pin the
two properties that fix that: a pid is only believed alongside the instance and
kernel start-time recorded with it, and the operator's intent to keep a strategy
running is stored separately from the pid so a crash is recoverable.

The runtime store is keyed by (name, mode): DRY and LIVE are separate books
with separate rows, so stopping one never touches the other.
"""
import os

import pytest

import src.utils.strategy_runtime as rt


async def test_a_pid_from_a_previous_instance_is_not_a_live_strategy(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("ai_directional", os.getpid(), "paper", "cli.py run")
    row = (await store.snapshot())[rt.key("ai_directional", "paper")]
    assert rt.owns_process(row) is True

    stale = dict(row, instance="some-earlier-instance")
    assert rt.owns_process(stale) is False


async def test_a_recycled_pid_is_not_mistaken_for_the_strategy(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("quick_flip", os.getpid(), "paper", "cli.py run")
    row = (await store.snapshot())[rt.key("quick_flip", "paper")]

    real = rt.process_start_ticks(os.getpid())
    if real is None:
        pytest.skip("kernel start-time ticks unavailable on this platform")
    assert rt.owns_process(dict(row, start_ticks=real + 999)) is False


async def test_intent_to_run_outlives_the_process(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("market_making", os.getpid(), "paper", "cli.py run")
    assert rt.key("market_making", "paper") in await store.desired()

    await store.record_stop("market_making", "exited on its own", "paper")
    await store.set_desired("market_making", True, "paper")
    assert rt.key("market_making", "paper") in await store.desired()

    await store.set_desired("market_making", False, "paper")
    assert rt.key("market_making", "paper") not in await store.desired()


async def test_stopping_one_book_never_touches_the_other(tmp_path):
    """DRY and LIVE are factually separate books.

    A single runtime row per strategy meant Stop on the LIVE page cleared the
    only record, so DRY stopped too. Each book now has its own row, and a stop
    scoped to one book leaves the other book's pid and desired flag untouched.
    """
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("btc_updown", os.getpid(), "paper", "cli.py run")
    await store.record_start("btc_updown", os.getpid(), "live", "cli.py run --live")

    await store.record_stop("btc_updown", "stopped by operator", "live")

    live_row = (await store.snapshot())[rt.key("btc_updown", "live")]
    assert not live_row["pid"]
    assert live_row["stop_reason"] == "stopped by operator"

    dry_row = (await store.snapshot())[rt.key("btc_updown", "paper")]
    assert dry_row["pid"], "stopping LIVE must not stop the DRY process"
    assert rt.key("btc_updown", "paper") in await store.desired()


async def test_a_heartbeat_is_the_living_proof_the_page_shows(tmp_path):
    """Running must mean the strategy's own loop stamped recently.

    A pid can be recycled or zombie; only a heartbeat written by the strategy
    process itself proves it factually executed a cycle. The runtime store
    stamps it, and a fresh start clears the old stamp so a respawned process is
    never marked stuck by its predecessor's silence.
    """
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("btc_updown", os.getpid(), "paper", "cli.py run --btc-updown")
    row = (await store.snapshot())[rt.key("btc_updown", "paper")]
    assert row.get("heartbeat_at") is None  # no fake proof before the first cycle

    await store.record_heartbeat("btc_updown", "paper")
    row = (await store.snapshot())[rt.key("btc_updown", "paper")]
    assert row.get("heartbeat_at")

    # A restart must clear the stale stamp, not inherit it.
    await store.record_start("btc_updown", os.getpid(), "paper", "cli.py run --btc-updown")
    row = (await store.snapshot())[rt.key("btc_updown", "paper")]
    assert row.get("heartbeat_at") is None


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
    row = (await store.snapshot())[rt.key("beast_mode", "paper")]
    assert row["instance"] == rt.INSTANCE


async def test_a_stopped_strategy_is_not_live(tmp_path):
    store = rt.StrategyRuntime(db_path=str(tmp_path / "t.db"))
    await store.record_start("safe_compounder", os.getpid(), "paper", "cli.py run")
    await store.record_stop("safe_compounder", "stopped by operator", "paper")
    row = (await store.snapshot())[rt.key("safe_compounder", "paper")]
    assert rt.owns_process(row) is False
    assert row["stop_reason"] == "stopped by operator"


async def test_the_old_name_keyed_table_is_rebuilt_composite(tmp_path):
    """A volume DB from the old single-row schema must survive the upgrade."""
    import aiosqlite

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(
            """
            CREATE TABLE strategy_runtime (
                name TEXT PRIMARY KEY, pid INTEGER, started_at TEXT, mode TEXT,
                command TEXT, stop_reason TEXT, stopped_at TEXT, instance TEXT,
                start_ticks INTEGER, desired INTEGER DEFAULT 0, heartbeat_at TEXT
            );
            INSERT INTO strategy_runtime (name, pid, mode, desired)
                VALUES ('ai_directional', 1234, 'paper', 1),
                       ('safe_compounder', NULL, 'dry', 0);
            """
        )
        await conn.commit()

    store = rt.StrategyRuntime(db_path=path)
    snap = await store.snapshot()
    assert rt.key("ai_directional", "paper") in snap
    # 'dry' normalises to 'paper', and a stopped lane stays stopped.
    assert rt.key("safe_compounder", "paper") in snap
    assert snap[rt.key("safe_compounder", "paper")]["desired"] == 0
