"""Persisted strategy process state.

The dashboard used to keep every strategy's pid in a module-level dict. That is
wrong in two ways that both showed up as a lie on the page:

1. The dict is per-process. gunicorn is pinned to one worker today, but any
   future worker - or a threaded reload, or the dev server versus the WSGI
   server - answers from its own copy, so a strategy started through one request
   is invisible to the next.
2. Nothing recorded *why* a child stopped. When a strategy exited, its row just
   read "stopped", which is indistinguishable from never having been started.
   The user saw "stopped" for strategies that were demonstrably trading.

So the pid lives in the database next to the rest of the runtime state, and the
liveness probe is platform-safe: `os.kill(pid, 0)` is a no-op signal on POSIX,
but on Windows any signal other than the CTRL_* events maps to TerminateProcess,
so probing with 0 there would kill the process being asked about.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_runtime (
    name TEXT PRIMARY KEY,
    pid INTEGER,
    started_at TEXT,
    mode TEXT,
    command TEXT,
    stop_reason TEXT,
    stopped_at TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pid_alive(pid: Optional[int]) -> bool:
    """True if a process with this pid exists, without signalling it.

    On Windows `os.kill(pid, 0)` would terminate the process, so the check goes
    through OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION instead.
    """
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except (OSError, ValueError):
        return False


def exit_code(pid: Optional[int]) -> Optional[int]:
    """Exit status of a finished child, when the OS will still tell us.

    Only meaningful on POSIX, where waitid() can read a non-child's status;
    returns None everywhere else rather than blocking or guessing.
    """
    if not pid or os.name == "nt":
        return None
    try:
        # os.WNOHANG is not exposed on every platform's os module.
        wnohang = getattr(os, "WNOHANG", 1)
        result = os.waitpid(int(pid), wnohang)
        return result[1] >> 8 if result[0] else None
    except (ChildProcessError, OSError):
        return None


@asynccontextmanager
async def _conn(db_path: str) -> AsyncIterator[Any]:
    """Open a connection with the table guaranteed.

    `aiosqlite.connect(...)` must not be awaited before `async with`: the
    awaitable starts the worker thread, and __aenter__ would start it again.
    """
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        yield conn


def run(coro: Any, timeout: float = 10.0) -> Any:
    """Run a coroutine on a bounded loop (Flask handlers are synchronous)."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
    finally:
        try:
            asyncio.set_event_loop(None)
        finally:
            loop.close()


class StrategyRuntime:
    """DB-backed record of which strategies are running, and why they stopped."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def record_start(self, name: str, pid: int, mode: str, command: str) -> Dict[str, Any]:
        async with _conn(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO strategy_runtime (name, pid, started_at, mode, command,"
                " stop_reason, stopped_at) VALUES (?,?,?,?,?,NULL,NULL)"
                " ON CONFLICT(name) DO UPDATE SET pid=excluded.pid,"
                " started_at=excluded.started_at, mode=excluded.mode,"
                " command=excluded.command, stop_reason=NULL, stopped_at=NULL",
                (name, int(pid), _now(), mode, command),
            )
            await conn.commit()
        return {"name": name, "pid": int(pid), "running": True}

    async def record_stop(self, name: str, reason: str) -> None:
        async with _conn(self.db_path) as conn:
            await conn.execute(
                "UPDATE strategy_runtime SET pid=NULL, stop_reason=?, stopped_at=?" " WHERE name=?",
                (reason, _now(), name),
            )
            await conn.commit()

    async def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Recorded state for every strategy that has ever been started."""
        try:
            async with _conn(self.db_path) as conn:
                cur = await conn.execute("SELECT * FROM strategy_runtime")
                return {r["name"]: dict(r) for r in await cur.fetchall()}
        except Exception:  # noqa: BLE001 - a fresh DB has no table yet
            return {}
