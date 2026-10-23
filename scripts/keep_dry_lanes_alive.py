"""Persistent keep-alive supervisor for the DRY (paper) lanes.

Runs forever and keeps every operator-armed DRY lane actually running, the
same job the web dashboard's _strategy_supervisor_loop does for the book the
page is in - but without needing the dashboard process itself. Pair this with
a boot hook (e.g. Termux:Boot) for lanes that come back after a device reboot.

The operator's button law is honoured exactly as in web_dashboard.py:

  * NO AUTO-START, EVER. A lane is only ever spawned if its strategy_runtime
    row already says desired=1 (paper). This daemon never arms a lane; arming
    happens via scripts/turn_on_dry_lanes.py or a dashboard Start press.
  * NO AUTO-KILL, EXCEPT an explicit operator Stop. A row with desired=0 and
    an operator stop_reason is terminated; a stale heartbeat is only logged.
    (Mirrors: "the operator's button is LAW... Never auto-kill a strategy.")
  * BOOK-SCOPED. It manages only mode='paper' rows, and only while the
    persisted book mode (runtime_config.trading_mode) is 'dry'. If the book
    ever flips to live, the daemon stands down instead of fighting the
    heartbeat validator.
  * quick_flip and safe_compounder were removed by operator order; they
    have no LANES entry and are never touched.
  * If the web dashboard is running on this host, the daemon yields: the
    dashboard's supervisor owns the lanes and both managers running at once
    would double-spawn.

Spawn contract matches web_dashboard._spawn_strategy: same interpreter, same
flags, child env scrubbed of TRADING_MODE/LIVE_TRADING_ENABLED,
STRATEGY_BOOK_MODE=paper and STRATEGY_NAME stamped, stdout+stderr appended to
logs/strategy_<name>.log, detached session, and the spawn persisted via
StrategyRuntime.record_start so liveness is provable from the DB.

Single-instance via a pidfile (logs/keep_dry_lanes_alive.pid).

Usage:
  python3 scripts/keep_dry_lanes_alive.py           # run in foreground
  python3 scripts/keep_dry_lanes_alive.py --once    # one pass, then exit
"""
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime

from turn_on_dry_lanes import (
    BASE_DIR,
    DB_PATH,
    LANES,
    LOG_DIR,
    child_env,
    pid_alive,
    row_alive,
)

PIDFILE = LOG_DIR / "keep_dry_lanes_alive.pid"
CYCLE_SEC = 10
HEARTBEAT_STALE_SEC = 120
OPERATOR_STOP_REASONS = frozenset(
    {"stopped by operator", "stopped by app restart and by operator"}
)
# Per-lane spawn-failure backoff: retries never stop, they slow to a ceiling.
MAX_BACKOFF = 30.0
_failures: dict = {}


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _dashboard_running() -> bool:
    """True if the web dashboard (or gunicorn serving it) is alive on this
    host. When it is, its own supervisor owns the lanes and this daemon must
    not spawn - two managers would double every lane."""
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            raw = open(f"/proc/{d}/cmdline", "rb").read()
        except OSError:
            continue
        if b"web_dashboard" in raw or b"gunicorn" in raw:
            return True
    return False


def _single_instance() -> bool:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if PIDFILE.exists():
            old = PIDFILE.read_text().strip()
            if old and pid_alive(int(old)):
                # Only a different keep-alive counts; the pid could be ours on
                # a restart race, but we are not in the pidfile yet.
                return False
    except (OSError, ValueError):
        pass
    try:
        PIDFILE.write_text(str(os.getpid()))
    except OSError:
        pass
    return True


async def _stop_lane(pid: int) -> None:
    try:
        os.kill(int(pid), 15)  # SIGTERM
    except (OSError, ValueError):
        pass


async def _spawn(name: str, args: list) -> None:
    from src.utils.strategy_runtime import StrategyRuntime, key as rt_key

    store = StrategyRuntime(DB_PATH)
    cmd = [sys.executable, "cli.py", *args]
    out = open(LOG_DIR / f"strategy_{name}.log", "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=child_env(name),
            start_new_session=True,
        )
    finally:
        out.close()
    # Persist BEFORE reporting; an untracked process is worse than none
    # (same rule as _spawn_strategy).
    last_err = None
    for attempt in range(8):
        try:
            await store.record_start(name, proc.pid, "paper", " ".join(cmd[1:]), desired=True)
            last_err = None
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.2 * (2.4 ** attempt))
    if last_err is not None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        log(f"{name}: FAILED to record ({last_err}); child stopped")
        raise RuntimeError(f"record failed for {name}")
    log(f"{name}: respawned (pid {proc.pid})")
    _failures.pop(name, None)


async def _cycle() -> None:
    from src.utils.mode import TradingMode
    from src.utils.strategy_runtime import StrategyRuntime, key as rt_key

    mode_mgr = TradingMode(DB_PATH)
    store = StrategyRuntime(DB_PATH)

    book = await mode_mgr.current()
    if book != "dry":
        log(f"book mode is {book!r}, not dry - standing down this cycle")
        return

    snapshot = await store.snapshot(mode="paper")
    for name, args in LANES.items():
        row = snapshot.get(rt_key(name, "paper")) or {}
        desired = int(row.get("desired") or 0)
        pid = row.get("pid")

        if not desired:
            # Operator Stop is permanent law. A disarmed lane that is somehow
            # still running is terminated - the only kill this daemon performs.
            if row_alive(row) and (row.get("stop_reason") or "") in OPERATOR_STOP_REASONS:
                log(f"{name}: operator stopped it - terminating pid {pid}")
                await _stop_lane(pid)
            continue

        if row_alive(row):
            hb = row.get("heartbeat_at")
            if hb:
                try:
                    age = time.time() - datetime.fromisoformat(hb).timestamp()
                    if age > HEARTBEAT_STALE_SEC:
                        log(f"{name}: heartbeat stale ({age:.0f}s) - alerting, not killing")
                except ValueError:
                    pass
            continue

        backoff = _failures.get(name, 0.0)
        if backoff > 0:
            log(f"{name}: down; retrying in {backoff:.0f}s")
            time.sleep(min(backoff, MAX_BACKOFF))
        try:
            await _spawn(name, args)
        except Exception as exc:  # noqa: BLE001 - retry forever, never die
            _failures[name] = min(max(_failures.get(name, 1.0) * 2, 2.0), MAX_BACKOFF)
            log(f"{name}: spawn failed ({type(exc).__name__}: {exc}); next retry in {_failures[name]:.0f}s")


async def main() -> None:
    once = "--once" in sys.argv
    if not _single_instance():
        log("another keep_dry_lanes_alive is already running - exiting")
        return
    log(f"keep-alive started (pid {os.getpid()}, once={once})")
    while True:
        try:
            if _dashboard_running():
                # Yield: the dashboard's supervisor owns the lanes.
                # If the dashboard started this process, we've been killed.
                # The operator's "i am the only one to turn on/off" requirement
                # means we should exit so the operator has exclusive control.
                log("dashboard already running - exiting to give operator exclusive control")
                return
            await _cycle()
        except Exception as exc:  # noqa: BLE001 - the keeper must never die
            log(f"cycle error: {type(exc).__name__}: {exc}")
        if once:
            return
        await asyncio.sleep(CYCLE_SEC)


if __name__ == "__main__":
    asyncio.run(main())
