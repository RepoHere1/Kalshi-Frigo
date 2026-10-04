"""The worker must not run out of threads.

This was the single cause behind everything that looked like the bot "turning
itself off":

    Supervisor could not start btc_updown: BlockingIOError: [Errno 11]
    Snapshot rebuild failed, serving last known: can't start new thread
    Mode payload: can't start new thread

`_run_async` built a throwaway event loop per call, and each aiosqlite connection
starts a thread. The dashboard makes many database calls per page, so threads
accumulated until the worker hit its limit. After that:

- the snapshot raised, so the client got nothing and every figure fell to zero
- buttons looked dead and clicks looked ignored, because requests hung
- the supervisor could not spawn strategies, because it could not start a thread
  to record them - so a stopped strategy genuinely stayed stopped

None of that is a trading fault and none of it is Railway restarting anything.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import web_dashboard as wd


def test_hundreds_of_snapshot_builds_do_not_accumulate_threads(tmp_path):
    """The regression, measured rather than asserted."""
    monkey = wd.DB_PATH
    wd.DB_PATH = str(tmp_path / "threads.db")
    wd.app.config["TESTING"] = False
    try:
        before = threading.active_count()

        def hit(_):
            wd.build_snapshot_cached()

        # Concurrent, because request handlers are threads.
        with ThreadPoolExecutor(max_workers=8) as pool:
            for _ in range(40):
                list(pool.map(hit, range(8)))

        time.sleep(0.5)
        after = threading.active_count()
    finally:
        wd.DB_PATH = monkey
        wd.app.config["TESTING"] = True

    assert after - before <= 4, (
        f"threads grew from {before} to {after} over 320 snapshot builds; "
        f"the per-call event loop is leaking"
    )


def test_run_async_reuses_one_loop():
    """One loop for the process, not one per call."""
    first = wd._async_loop()
    second = wd._async_loop()
    assert first is second
    assert not first.is_closed()


def test_run_async_works_from_a_worker_thread():
    """Request handlers are not on the main thread; this must still work."""
    import asyncio

    async def answer():
        await asyncio.sleep(0)
        return 42

    out = []
    t = threading.Thread(target=lambda: out.append(wd._run_async(answer())))
    t.start()
    t.join(30)
    assert out == [42]


def test_run_async_still_refuses_a_nested_loop():
    """Calling it from inside a running loop is a bug and must stay loud."""
    import asyncio

    async def inner():
        async def answer():
            return 1

        coro = answer()
        try:
            with pytest.raises(RuntimeError):
                wd._run_async(coro)
        finally:
            coro.close()

    wd._run_async(inner())


def test_run_async_is_bounded():
    """A wedged loop must surface, not hang a request forever."""
    import inspect

    src = inspect.getsource(wd._run_async)
    assert "timeout=" in src
    assert "future.result" in src


def test_every_module_shares_one_bridge_loop():
    """mode.run and strategy_runtime.run leaked loops the same way.

    The dashboard's `_run_async` was fixed, but `mode.run` and
    `strategy_runtime.run` each still built a throwaway loop per call - and the
    mode payload calls them several times per page poll. All three now submit
    to the one process-wide bridge loop.
    """
    import asyncio

    from src.utils import async_bridge
    from src.utils.mode import run as mode_run
    from src.utils.strategy_runtime import run as rt_run

    async def answer():
        await asyncio.sleep(0)
        return 42

    assert wd._async_loop() is async_bridge._get_loop()
    assert mode_run(answer()) == 42
    assert rt_run(answer()) == 42
    assert not async_bridge._get_loop().is_closed()
