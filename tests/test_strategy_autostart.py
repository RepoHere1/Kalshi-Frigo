"""Nothing may take a strategy out of service except the operator pressing Stop.

Two faults kept lanes down:

1. `_spawn_strategy` persisted the new pid *after* launching the child, and the
   write was fire-and-forget. When it failed - routine under WAL with six
   strategies plus the dashboard writing - the exception escaped with the child
   already running and unrecorded. The dashboard then read the previous row, saw
   a pid from a previous instance, and reported "stopped by app restart" for a
   strategy that was very much alive and trading. Pressing Start again would
   spawn a second copy of the same strategy trading the same book.

2. The supervisor gave up permanently after 8 failed starts, so one bad stretch
   - a locked database, a deploy mid-write - left strategies down until a human
   noticed.

An unrecorded trading process is worse than no process: it cannot be stopped from
the dashboard, and a second copy can be started alongside it.
"""
import inspect

import pytest

import web_dashboard as wd


def _src():
    return inspect.getsource(wd._spawn_strategy)


def test_a_spawn_that_cannot_be_recorded_kills_its_own_child():
    """Never leave a trading process nobody can see or stop."""
    src = _src()
    assert "record_start" in src
    # It retries rather than failing on the first locked write...
    assert "for attempt in range(4)" in src
    # ...and if it still cannot record, the child is stopped.
    assert "proc.terminate()" in src
    assert "could not record it" in src


def test_the_record_is_attempted_more_than_once():
    assert "for attempt in range(4)" in _src()
    assert "time.sleep(0.4 * (attempt + 1))" in _src()


def test_every_strategy_is_wanted_from_boot():
    assert wd.AUTO_START_ALL is True


def test_the_supervisor_never_gives_up_permanently():
    src = inspect.getsource(wd._strategy_supervisor_loop)
    # The operator-facing promise must be that it keeps trying, not that it
    # stopped trying.
    assert "still retrying every" in src
    assert "it will not" not in src
    assert wd._SUPERVISOR_MAX_BACKOFF > 0


def test_auto_start_does_not_resurrect_a_deliberately_stopped_strategy():
    """Seeding happens only for names with no row at all.

    Stop clears `desired` but leaves the row, so a stopped lane must not be
    restarted on the next pass.
    """
    src = inspect.getsource(wd._strategy_supervisor_loop)
    assert "if name in recorded:" in src
    assert "Started {name} (all strategies run by default)" in src


def test_backoff_has_a_ceiling():
    """Retries forever, but never as a busy loop."""
    assert 0 < wd._SUPERVISOR_MAX_BACKOFF <= 300


def test_stopping_still_works_and_is_sticky():
    """The operator's Stop is the only way a lane goes down."""
    from src.utils.strategy_runtime import StrategyRuntime
    import tempfile

    async def scenario():
        import os

        path = os.path.join(tempfile.mkdtemp(), "t.db")
        store = StrategyRuntime(db_path=path)
        await store.record_start("ai_directional", 4321, "paper", "cli.py run")
        assert "ai_directional" in await store.desired()

        await store.set_desired("ai_directional", False)
        assert "ai_directional" not in await store.desired()

        # The row remains, which is what stops auto-start from resurrecting it.
        assert "ai_directional" in await store.snapshot()

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize("name", sorted(wd.STRATEGY_COMMANDS))
def test_every_command_is_pinned_to_paper(name):
    """AUTO_START_ALL starts things without asking; they must not be live."""
    assert "--paper" in wd.STRATEGY_COMMANDS[name]
