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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from src.utils.database import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_runtime (
    name TEXT NOT NULL,
    pid INTEGER,
    started_at TEXT,
    mode TEXT NOT NULL,
    command TEXT,
    stop_reason TEXT,
    stopped_at TEXT,
    instance TEXT,
    start_ticks INTEGER,
    desired INTEGER DEFAULT 0,
    heartbeat_at TEXT,
    PRIMARY KEY (name, mode)
);
"""


def key(name: str, mode: str) -> str:
    """The composite record key: DRY and LIVE are separate books.

    One row per strategy used to mean ONE process per strategy, so stopping it
    in LIVE stopped it in DRY too - the two books shared a single runtime
    record and therefore a single on/off switch. Each book now has its own row,
    its own pid, its own desired flag: Stop on the LIVE page touches only the
    LIVE row, and DRY keeps running whatever DRY was told to do.
    """
    return f"{name}|{mode}"

# A process tree does not outlive the container. Every pid recorded by a previous
# instance of this app therefore names something else now - at best nothing, at
# worst an unrelated process that happens to have been assigned the same number.
# The DB lives on a Railway volume and survives redeploys; these pids do not.
# Stamping each row with the instance that created it is what makes a recorded pid
# mean anything at all.
INSTANCE = uuid.uuid4().hex


async def _migrate_composite_key(conn: Any) -> None:
    """Rebuild strategy_runtime so (name, mode) is the primary key.

    The old table was keyed on `name` alone, which made DRY and LIVE share one
    record and one on/off switch. `CREATE TABLE IF NOT EXISTS` leaves the old
    table untouched, so the PK is detected and the table rebuilt: rows are
    copied with their mode normalised ('dry' -> 'paper', empty -> 'paper'), and
    a NULL/empty pid carries desired=0 forward so a lane stopped in the old
    schema stays stopped rather than coming back armed by accident.
    """
    cur = await conn.execute("PRAGMA index_list(strategy_runtime)")
    indexes = await cur.fetchall()
    pk_cols = None
    for idx in indexes:
        if int(idx[2]) == 1 and idx[3] == "pk":
            cur = await conn.execute(f"PRAGMA index_info({idx[1]})")
            # index_info rows are (seqno, cid, name): the NAME is index 2. Reading
            # index 1 returned column ordinals, so the composite PK was never
            # detected and the table was rebuilt on EVERY connection - which
            # reset desired=0 on stopped rows and made intent un-settable.
            pk_cols = [r[2] for r in await cur.fetchall()]
            break
    if pk_cols is not None and "mode" in pk_cols:
        return

    await conn.execute("ALTER TABLE strategy_runtime RENAME TO strategy_runtime_legacy")
    await conn.execute(
        """
        CREATE TABLE strategy_runtime (
            name TEXT NOT NULL,
            pid INTEGER,
            started_at TEXT,
            mode TEXT NOT NULL,
            command TEXT,
            stop_reason TEXT,
            stopped_at TEXT,
            instance TEXT,
            start_ticks INTEGER,
            desired INTEGER DEFAULT 0,
            heartbeat_at TEXT,
            PRIMARY KEY (name, mode)
        )
        """
    )
    await conn.execute(
        """
        INSERT INTO strategy_runtime
            (name, pid, started_at, mode, command, stop_reason, stopped_at,
             instance, start_ticks, desired, heartbeat_at)
        SELECT name,
               CASE WHEN pid IS NULL OR pid = 0 THEN NULL ELSE pid END,
               started_at,
               CASE WHEN NULLIF(mode, '') IS NULL OR mode = 'dry' THEN 'paper'
                    ELSE mode END,
               command, stop_reason, stopped_at, instance, start_ticks,
               CASE WHEN pid IS NULL OR pid = 0 THEN 0 ELSE COALESCE(desired, 0) END,
               heartbeat_at
        FROM strategy_runtime_legacy
        """
    )
    await conn.execute("DROP TABLE strategy_runtime_legacy")
    await conn.commit()


async def _ensure_columns(conn: Any) -> None:
    """Add columns introduced after the table first shipped.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so a volume
    that already holds a strategy_runtime table would otherwise never gain these
    columns and every query naming them would fail.
    """
    cur = await conn.execute("PRAGMA table_info(strategy_runtime)")
    existing = {row[1] for row in await cur.fetchall()}
    for name, ddl in (
        ("instance", "ALTER TABLE strategy_runtime ADD COLUMN instance TEXT"),
        ("start_ticks", "ALTER TABLE strategy_runtime ADD COLUMN start_ticks INTEGER"),
        ("desired", "ALTER TABLE strategy_runtime ADD COLUMN desired INTEGER DEFAULT 0"),
        ("heartbeat_at", "ALTER TABLE strategy_runtime ADD COLUMN heartbeat_at TEXT"),
    ):
        if name not in existing:
            await conn.execute(ddl)
    await conn.commit()


def process_start_ticks(pid: Optional[int]) -> Optional[int]:
    """The kernel's start-time for `pid`, or None if it cannot be read.

    Linux exposes this as field 22 of /proc/<pid>/stat. Combined with the pid it
    identifies one specific process: a recycled pid gets a different value, so a
    match proves the process we recorded is the process we are looking at.
    """
    if not pid or os.name == "nt":
        return None
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    # The comm field is parenthesised and may itself contain spaces or ')'.
    try:
        fields = raw[raw.rindex(b")") + 2 :].split()
        return int(fields[19])  # field 22 overall, after state at index 0
    except (ValueError, IndexError):
        return None


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

    async with connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await _ensure_columns(conn)
        await _migrate_composite_key(conn)
        yield conn


def run(coro: Any, timeout: float = 10.0) -> Any:
    """Run a coroutine on the process-wide bridge loop (see async_bridge.py).

    A throwaway loop per call leaked executor threads from aiosqlite until the
    worker could not start any thread at all, which is what made strategies
    look like they were turning themselves off.
    """
    from src.utils.async_bridge import run as _bridge_run

    return _bridge_run(coro, timeout=timeout)


class StrategyRuntime:
    """DB-backed record of which strategies are running, and why they stopped."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def record_start(
        self,
        name: str,
        pid: int,
        mode: str,
        command: str,
        desired: bool = True,
    ) -> Dict[str, Any]:
        async with _conn(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO strategy_runtime (name, pid, started_at, mode, command,"
                " stop_reason, stopped_at, instance, start_ticks, desired, heartbeat_at)"
                " VALUES (?,?,?,?,?,NULL,NULL,?,?,?,NULL)"
                " ON CONFLICT(name, mode) DO UPDATE SET pid=excluded.pid,"
                " started_at=excluded.started_at,"
                " command=excluded.command, stop_reason=NULL, stopped_at=NULL,"
                " instance=excluded.instance, start_ticks=excluded.start_ticks,"
                " desired=excluded.desired, heartbeat_at=NULL",
                (
                    name,
                    int(pid),
                    _now(),
                    mode,
                    command,
                    INSTANCE,
                    process_start_ticks(pid),
                    1 if desired else 0,
                ),
            )
            await conn.commit()
        return {"name": name, "pid": int(pid), "mode": mode, "running": True}

    async def record_stop(
        self, name: str, reason: str, mode: str, clear_desired: bool = True
    ) -> None:
        # The mode scopes the stop to ONE BOOK: stopping LIVE must never touch
        # DRY. `clear_desired` distinguishes the two kinds of stop:
        #
        #   clear_desired=True  - the operator pressed Stop. This is PERMANENT
        #     intent: the lane stays down across redeploys, book switches and
        #     any number of logins, until Start is pressed in this book again.
        #   clear_desired=False - the process died on its own (crash, restart).
        #     That is not a decision, so the operator's intent is kept and the
        #     supervisor resurrects the lane.
        async with _conn(self.db_path) as conn:
            if clear_desired:
                await conn.execute(
                    "UPDATE strategy_runtime SET pid=NULL, stop_reason=?, stopped_at=?,"
                    " instance=?, desired=0"
                    " WHERE name=? AND mode=?",
                    (reason, _now(), INSTANCE, name, mode),
                )
            else:
                await conn.execute(
                    "UPDATE strategy_runtime SET pid=NULL, stop_reason=?, stopped_at=?,"
                    " instance=?"
                    " WHERE name=? AND mode=?",
                    (reason, _now(), INSTANCE, name, mode),
                )
            await conn.commit()

    async def record_heartbeat(self, name: str, mode: str) -> None:
        """Stamp that the strategy process is factually alive right now.

        This is the constant proof the dashboard shows: a pid can be recycled or
        zombie without trading, but a fresh heartbeat means the strategy's own
        loop executed a cycle since the stamp. Written by the strategy process
        itself every cycle; read by the dashboard, which refuses to call a
        strategy "running" once the stamp goes stale.
        """
        async with _conn(self.db_path) as conn:
            await conn.execute(
                "UPDATE strategy_runtime SET heartbeat_at=? WHERE name=? AND mode=?",
                (_now(), name, mode),
            )
            await conn.commit()

    async def set_desired(self, name: str, desired: bool, mode: str) -> None:
        """Record the operator's intent for ONE BOOK, independent of any pid.

        This is what "run until I say otherwise" means concretely: the intent
        outlives the process, so a crash or a restart can be recovered from
        without the operator having to notice and press Start again.
        """
        async with _conn(self.db_path) as conn:
            await conn.execute(
                "UPDATE strategy_runtime SET desired=? WHERE name=? AND mode=?",
                (1 if desired else 0, name, mode),
            )
            await conn.commit()

    async def desired(self, mode: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Every (strategy, book) pair the operator asked to keep running.

        Keys are composite (`name|mode`). `mode` filters to one book.
        """
        try:
            async with _conn(self.db_path) as conn:
                if mode:
                    cur = await conn.execute(
                        "SELECT * FROM strategy_runtime WHERE desired = 1 AND mode = ?",
                        (mode,),
                    )
                else:
                    cur = await conn.execute("SELECT * FROM strategy_runtime WHERE desired = 1")
                return {key(r["name"], r["mode"]): dict(r) for r in await cur.fetchall()}
        except Exception:  # noqa: BLE001 - a fresh DB has no table yet
            return {}

    async def snapshot(self, mode: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Recorded state for every (strategy, book) that has ever been started."""
        try:
            async with _conn(self.db_path) as conn:
                if mode:
                    cur = await conn.execute(
                        "SELECT * FROM strategy_runtime WHERE mode = ?", (mode,)
                    )
                else:
                    cur = await conn.execute("SELECT * FROM strategy_runtime")
                return {key(r["name"], r["mode"]): dict(r) for r in await cur.fetchall()}
        except Exception:  # noqa: BLE001 - a fresh DB has no table yet
            return {}


def owns_process(row: Dict[str, Any]) -> bool:
    """True only if the recorded pid is still the exact process we started.

    Three independent reasons to say no, each of which used to read as "running":
    the row was written by a previous instance of the app (so the pid names
    something else entirely), the pid is gone, or the pid has been recycled by an
    unrelated process (its kernel start-time no longer matches).
    """
    pid = row.get("pid")
    if not pid:
        return False
    if (row.get("instance") or "") != INSTANCE:
        return False
    if not pid_alive(pid):
        return False
    recorded_ticks = row.get("start_ticks")
    if recorded_ticks is not None:
        current = process_start_ticks(pid)
        if current is not None and current != recorded_ticks:
            return False
    return True
