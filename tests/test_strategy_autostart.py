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
    assert "for attempt in range(8)" in src
    # ...and if it still cannot record, the child is stopped.
    assert "proc.terminate()" in src
    assert "could not record it" in src


def test_the_record_is_attempted_more_than_once():
    assert "for attempt in range(8)" in _src()
    # Exponential backoff between record attempts.
    assert "backoff = 0.2 * (2.4 ** attempt)" in _src()
    assert "time.sleep(backoff)" in _src()


def test_nothing_is_wanted_from_boot_without_the_operator():
    """The operator's button is the only arming authority.

    AUTO_START_ALL was killed: no lane may come into being because
    a process booted. Every running lane exists because the operator
    pressed Start in that book.
    """
    assert wd.AUTO_START_ALL is False


def test_the_supervisor_never_gives_up_permanently():
    src = inspect.getsource(wd._strategy_supervisor_loop)
    # The operator-facing promise must be that it keeps trying, not that
    # it stopped trying.
    assert "forever until STOP button pressed" in src
    assert "it will not" not in src
    assert wd._SUPERVISOR_MAX_BACKOFF > 0


def test_auto_start_does_not_resurrect_a_deliberately_stopped_strategy():
    """Seeding happens only for names with no row at all.

    Stop clears `desired` but leaves the row, so a stopped lane must not be
    restarted on the next pass.
    """
    src = inspect.getsource(wd._strategy_supervisor_loop)
    assert "_OPERATOR_STOP_REASONS" in src
    assert "recorded.get(_runtime_key(name))" in src
    assert "all strategies run by default" in src
    # ...and it must start into the book actually in force, not a hardcoded one.
    assert "book_mode = _runtime_mode()" in src
    assert '_spawn_strategy(name, "paper")' not in src


def test_the_supervisor_touches_only_the_book_it_runs_in():
    """DRY and LIVE are separate books; the supervisor manages one of them.

    Every read and write is scoped with the book's mode word, so a Stop given
    on the LIVE page can never cascade into DRY.
    """
    src = inspect.getsource(wd._strategy_supervisor_loop)
    assert "book_mode = _runtime_mode()" in src
    assert "store.desired(mode=book_mode)" in src
    assert "store.snapshot(mode=book_mode)" in src
    assert "store.set_desired(name, True, book_mode)" in src


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
def test_a_restart_re_arms_every_dry_lane_and_only_pushed_live_lanes():
    """A redeploy must not silently stop the DRY book - and must never ARM live.

    THE LAW: the operator's button pushes are the only facts. AUTO-START is a
    DRY-book convenience because DRY cannot lose money; the LIVE book trades
    real money, so a LIVE lane may come into being ONLY by an explicit push in
    the LIVE book. Pushed LIVE lanes are still honoured across restarts - the
    supervisor's resume path reads `desired(mode='live')` rows - but booting
    while the book reads LIVE seeds nothing by itself.
    """
    src = inspect.getsource(wd._strategy_supervisor_loop)
    # AUTO-START is gated to the DRY book only.
    assert 'if AUTO_START_ALL and book_mode == "dry":' in src
    # The book in force still decides which book a lane is armed into.
    assert "book_mode = _runtime_mode()" in src
    assert '_spawn_strategy(name, "paper")' not in src


def test_the_supervisor_never_spawns_on_an_unreadable_book():
    """A guessed book armed the wrong book once; None means NO book.

    `_current_book_mode` failing open to 'dry' let one worker answer 'dry'
    while another read 'live' during boot contention, and strategy processes
    got armed in a book the operator never pushed. The supervisor must skip
    its cycle entirely when the book cannot be read.
    """
    src = inspect.getsource(wd._strategy_supervisor_loop)
    assert "if book_mode is None:" in src
    assert "continue" in src


def test_the_toggle_refuses_to_act_on_an_unreadable_book():
    """Start/Stop must never guess which book they are for."""
    src = inspect.getsource(wd.api_strategy_toggle)
    assert "Trading book is unknown right now" in src
    assert "503" in src


def test_book_mode_fails_closed_not_open():
    """No mode read means None - never a silent 'dry' a write could trust."""
    src = inspect.getsource(wd._current_book_mode)
    assert "return None" in src
    assert 'return "dry"' not in src


def test_bots_payload_reports_only_this_books_lanes():
    """The other book's lanes must not render as running on this page.

    Listing LIVE lanes while the page was in DRY is exactly what made a DRY
    Start look like it had switched the LIVE LLM strategy on.
    """
    src = inspect.getsource(wd._bots_payload)
    assert "_runtime_store().snapshot(mode=book)" in src
    assert "THIS BOOK" in src


def test_an_operator_stop_is_permanent_across_instances():
    """The operator's Stop is welded to the state forever.

    A stop must NOT expire with the process that recorded it: the button is
    the truth. A lane stopped in one instance stays stopped across redeploys
    and logins until Start is pressed in that book again - there is no
    instance check, no expiry window, nothing that can quietly re-arm it.
    """
    from src.utils.strategy_runtime import StrategyRuntime

    supervisor = inspect.getsource(wd._strategy_supervisor_loop)
    assert "_OPERATOR_STOP_REASONS" in supervisor
    # No instance scoping on the operator-stop exemption any more.
    assert "_CUR_INSTANCE" not in supervisor

    stop_src = inspect.getsource(StrategyRuntime.record_stop)
    assert "clear_desired" in stop_src
    assert "desired=0" in stop_src


def test_a_crash_does_not_clear_the_operators_intent():
    """A died-on-its-own lane must be resurrected, not permanently stopped.

    record_stop distinguishes the operator's Stop (clears desired forever)
    from an incidental death (keeps desired), so a crash can never weld a
    lane off.
    """
    from src.utils.strategy_runtime import StrategyRuntime

    src = inspect.getsource(StrategyRuntime.record_stop)
    assert "clear_desired: bool = True" in src
    assert "clear_desired" in src
    dash_src = inspect.getsource(wd._recorded_state)
    assert "clear_desired=False" in dash_src


def test_the_live_page_describes_the_real_arming_policy():
    """The page must not promise "nothing here starts by itself".

    That sentence described the old gate. Leaving it in place after the gate was
    removed is the same class of bug this whole file is about: the UI asserting
    something the code no longer does.
    """
    html = wd._TEMPLATE
    assert "armed one strategy at a time" not in html
    assert "does not place a single order" not in html
    assert "remembers YOUR LAST CLICK forever" in html


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
    from src.utils.strategy_runtime import StrategyRuntime, key
    import tempfile

    async def scenario():
        import os

        path = os.path.join(tempfile.mkdtemp(), "t.db")
        store = StrategyRuntime(db_path=path)
        await store.record_start("ai_directional", 4321, "paper", "cli.py run")
        assert key("ai_directional", "paper") in await store.desired()

        await store.set_desired("ai_directional", False, "paper")
        assert key("ai_directional", "paper") not in await store.desired()

        # The row remains, which is what stops auto-start from resurrecting it.
        assert key("ai_directional", "paper") in await store.snapshot()

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize("name", sorted(wd.STRATEGY_COMMANDS))
def test_every_command_is_pinned_to_paper(name):
    """AUTO_START_ALL starts things without asking; they must not be live."""
    assert "--paper" in wd.STRATEGY_COMMANDS[name]


# ---------------------------------------------------------------------------
# The BTC keep-alive obeys the button
# ---------------------------------------------------------------------------
def test_btc_keepalive_never_overrides_an_operator_stop():
    """The button is law, even for the always-on BTC lanes.

    The keep-alive keeps the three UP/DOWN lanes on, but a lane
    the operator stopped in this book stays down until Start is
    pressed here again. Only a crash (desired still 1) or a lane
    never pushed comes back up - those are not decisions.
    """
    src = inspect.getsource(wd._btc_keepalive_loop)
    # It reads the operator-stop reasons and the desired flag...
    assert "_OPERATOR_STOP_REASONS" in src
    assert 'row.get("desired") == 0' in src
    # ...and skips the lane before it can re-arm it.
    assert "continue" in src
    # A crashed lane (desired still 1) is still re-armed.
    assert "set_desired(name, True, book_mode)" in src


def test_start_all_only_starts_stopped_strategies():
    """Start all must not stop running strategies.

    toggleStrategy TOGGLES, so feeding it every card would STOP
    the running ones - the exact opposite of "Start all". It must
    only start the stopped ones and leave running lanes alone.
    """
    html = wd._TEMPLATE
    assert "filter(c => !c.running)" in html


def test_kill_all_is_a_distinct_immediate_kill():
    """Kill all is not Stop all.

    Kill all hard-kills every running process in the current book
    right now (no graceful SIGTERM wait) and records an operator
    stop so neither supervisor resurrects a killed lane.
    """
    html = wd._TEMPLATE
    assert "async function killAll()" in html
    assert 'onclick="killAll()"' in html
    kill_src = inspect.getsource(wd.api_strategies_kill)
    # Book-scoped, fail-safe on an unreadable book.
    assert "_current_book_mode()" in kill_src
    # Hard kill, not a graceful stop.
    assert "_kill_child" in kill_src
    # Recorded as an operator stop so it stays down.
    assert "record_stop" in kill_src
    assert "stopped by operator" in kill_src


def test_hard_kill_takes_a_process_down_now():
    """_kill_child SIGKILLs immediately, unlike _stop_child's grace."""
    src = inspect.getsource(wd._kill_child)
    assert "proc.kill()" in src
    assert "SIGKILL" in src
