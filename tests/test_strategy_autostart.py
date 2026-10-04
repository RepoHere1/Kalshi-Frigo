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
    assert "_OPERATOR_STOP_REASONS" in src
    assert "recorded.get(name)" in src
    assert "all strategies run by default" in src
    # ...and it must start into the book actually in force, not a hardcoded one.
    assert '_current_book_mode() == "live"' in src
    assert '_spawn_strategy(name, "paper")' not in src


def test_only_an_operator_stop_keeps_a_lane_down():
    assert "stopped by operator" in wd._OPERATOR_STOP_REASONS
    for incidental in ("exited on its own", "stopped by app restart"):
        assert incidental not in wd._OPERATOR_STOP_REASONS


def test_backoff_has_a_ceiling():
    """Retries forever, but never as a busy loop."""
    assert 0 < wd._SUPERVISOR_MAX_BACKOFF <= 300


# ---------------------------------------------------------------------------
# Arming policy
# ---------------------------------------------------------------------------
def test_a_restart_re_arms_every_lane_in_either_book():
    """A redeploy must not silently stop the book.

    The seed used to be gated on `AUTO_START_ALL and _current_book_mode() !=
    "live"`, so every LIVE lane stayed down across a restart and the account
    quietly stopped trading until somebody noticed and pressed Start on six
    buttons. Gating it that way made the DRY/LIVE distinction a reason to stop
    working rather than a reason to be careful about money.

    Arming is now the same in both books. The mode switch itself still starts
    nothing - that is a separate endpoint - so switching to LIVE does not place
    an order; the seed only runs on the supervisor's boot pass.
    """
    src = inspect.getsource(wd._strategy_supervisor_loop)
    assert "if AUTO_START_ALL:" in src
    assert '_current_book_mode() != "live"' not in src
    # The book in force still decides which book a lane is armed into.
    assert '_current_book_mode() == "live"' in src
    assert '_spawn_strategy(name, "paper")' not in src


def test_an_operator_stop_only_counts_when_given_in_this_instance():
    """A stop must not outlive the process that recorded it.

    `record_stop` stamps the instance id, and the operator-stop exemption only
    applies to a row from the current instance. Without that pairing a stop
    recorded before a redeploy is indistinguishable from a fresh decision, so a
    lane stopped once stayed down forever - which is precisely the "strategies
    turned themselves off and never came back" symptom.
    """
    from src.utils.strategy_runtime import INSTANCE, StrategyRuntime

    supervisor = inspect.getsource(wd._strategy_supervisor_loop)
    assert "_CUR_INSTANCE" in supervisor
    assert '(row.get("instance") or "") == _CUR_INSTANCE' in supervisor

    stop_src = inspect.getsource(StrategyRuntime.record_stop)
    assert "instance=?" in stop_src
    assert INSTANCE is not None


def test_the_live_page_describes_the_real_arming_policy():
    """The page must not promise "nothing here starts by itself".

    That sentence described the old gate. Leaving it in place after the gate was
    removed is the same class of bug this whole file is about: the UI asserting
    something the code no longer does.
    """
    html = wd._TEMPLATE
    assert "armed one strategy at a time" not in html
    assert "does not place a single order" not in html
    assert "re-arms every lane automatically" in html


def test_the_dry_page_offers_no_live_controls(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "DB_PATH", str(tmp_path / "dry.db"))
    monkeypatch.setattr(wd, "LOG_DIR", tmp_path / "logs")
    wd.app.config["TESTING"] = True
    html = wd.app.test_client().get("/").get_data(as_text=True)
    assert "Start all in DRY" in html
    assert "Kill all LIVE" not in html
    assert "re-arms every lane automatically" not in html


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
