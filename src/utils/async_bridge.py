"""One process-wide event loop for bridging synchronous code into async.

Every module that needed to run a coroutine from synchronous code used to build
a throwaway event loop per call. Two things made that fatal:

- aiosqlite runs its SQLite work through `loop.run_in_executor`, which lazily
  creates the loop's default executor; a raw `run_until_complete + close()`
  never shuts that executor down, so every call leaked its executor threads;
- the dashboard worker makes a mode/DB call on every page poll, so the leaked
  threads accumulated until the process hit the OS limit and every new thread
  failed with "can't start new thread" - the snapshot went blank, buttons died,
  and the supervisor could not spawn strategies because it could not start a
  thread to record them. Railway then restarted the container, which killed
  every strategy child process with it. That whole cascade is what looked like
  "strategies turning themselves off".

One loop, one thread, reused forever, submitted to with
`run_coroutine_threadsafe`.
"""
import asyncio
import threading
from typing import Any, Optional

_LOOP: Optional[asyncio.AbstractEventLoop] = None
_THREAD: Optional[threading.Thread] = None
_LOCK = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """The one process-wide loop, created lazily and never closed."""
    global _LOOP, _THREAD
    with _LOCK:
        if (
            _LOOP is not None
            and not _LOOP.is_closed()
            and _THREAD is not None
            and _THREAD.is_alive()
        ):
            return _LOOP

        loop = asyncio.new_event_loop()

        def _run_forever() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run_forever, daemon=True, name="async-bridge")
        thread.start()
        _LOOP, _THREAD = loop, thread
        return loop


def run(coro: Any, timeout: float = 30.0) -> Any:
    """Run `coro` on the process-wide loop and return its result.

    Raises RuntimeError when called from inside a running event loop - there the
    caller must await the coroutine directly instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "async_bridge.run() cannot be called from inside a running event loop; "
            "await the coroutine directly instead."
        )

    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(
        asyncio.wait_for(coro, timeout=timeout), loop
    )
    # Slack over the coroutine's own timeout for the cross-thread dispatch.
    return future.result(timeout=timeout + 5)
