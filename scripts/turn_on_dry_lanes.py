"""Turn on the DRY book lanes: persist DRY, arm, and spawn every lane.

Mirrors what the web dashboard's "Start all in DRY" + strategy supervisor do:

  1. Persist the book mode: runtime_config key `trading_mode` = 'dry'.
  2. Arm every lane: strategy_runtime row (name, mode='paper', desired=1,
     stop_reason cleared) via StrategyRuntime.arm() - the same call the
     /api/strategy/<name>/toggle endpoint makes on a Start press.
  3. Spawn each armed lane the same way web_dashboard._spawn_strategy does:
     same interpreter (sys.executable), same flags, child env scrubbed of
     TRADING_MODE/LIVE_TRADING_ENABLED, STRATEGY_BOOK_MODE='paper' and
     STRATEGY_NAME stamped, stdout+stderr appended to logs/strategy_<name>.log,
     detached session so the lanes outlive this script. Each spawn is
     persisted with StrategyRuntime.record_start (pid, started_at, instance,
     start_ticks) so liveness is provable from the DB.
  4. quick_flip was removed from the book (card, command, and strategy code)
     by operator order; nothing here can ever arm it.

DRY is the simulated book - no real orders are ever sent (should_trade_live()
in src/jobs/broker.py requires the persisted mode to be 'live').

NOTE for operators: if the web dashboard is booted on the same host while
these processes are still alive, its supervisor sees the recorded instance
stamp as foreign (a new boot gets a new INSTANCE uuid) and may respawn the
same lanes - creating double processes. Reconcile by pressing Stop once per
lane in the dashboard, or let the dashboard own the lanes from then on.

Usage:
  python3 scripts/turn_on_dry_lanes.py              # persist + arm + spawn
  python3 scripts/turn_on_dry_lanes.py --no-spawn   # persist + arm only
  python3 scripts/turn_on_dry_lanes.py --dry-run    # report, write nothing
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
DB_PATH = str(BASE_DIR / "trading_system.db")
LOG_DIR = BASE_DIR / "logs"

# Card names and commands from web_dashboard.strategy_state / STRATEGY_COMMANDS.
# quick_flip was removed by operator order.
LANES = {
    "btc_updown": ["run", "--btc-updown", "--series", "KXBTC15M", "--spot-product", "BTC-USD", "--paper", "--loop", "--interval", "0"],
    "xrp_updown": ["run", "--btc-updown", "--series", "KXXRP15M", "--spot-product", "XRP-USD", "--paper", "--loop", "--interval", "0"],
    "btc_1h_updown": ["run", "--btc-updown", "--series", "KXBTC1H", "--spot-product", "BTC-USD", "--paper", "--loop", "--interval", "0"],
    "eth_updown": ["run", "--btc-updown", "--series", "KXETH15M", "--spot-product", "ETH-USD", "--paper", "--loop", "--interval", "0"],
    "eth_1h_updown": ["run", "--btc-updown", "--series", "KXETH1H", "--spot-product", "ETH-USD", "--paper", "--loop", "--interval", "0"],
    "xrp_1h_updown": ["run", "--btc-updown", "--series", "KXXRP1H", "--spot-product", "XRP-USD", "--paper", "--loop", "--interval", "0"],
    "hyperliquid_updown": ["run", "--btc-updown", "--series", "KXHYPE15M", "--spot-product", "HYPE-USD", "--paper", "--loop", "--interval", "0"],
    "hyperliquid_1h_updown": ["run", "--btc-updown", "--series", "KXHYPED", "--spot-product", "HYPE-USD", "--paper", "--loop", "--interval", "0"],
    # ALL NO MARKETS lane: NO-side only maker entries, $250 book separate from DRY
    "all_no_markets": ["run", "--all-no-markets", "--paper", "--loop", "--interval", "0"],
}
LOCKED_OFF: set = set()


def child_env(name: str) -> dict:
    """The exact environment _spawn_strategy builds for a paper child."""
    env = os.environ.copy()
    env.pop("TRADING_MODE", None)
    env.pop("LIVE_TRADING_ENABLED", None)
    env["STRATEGY_BOOK_MODE"] = "paper"
    env["STRATEGY_NAME"] = name
    env["DB_PATH"] = DB_PATH
    # Enable ALL NO MARKETS strategy if this is the ALL NO lane
    if name == "all_no_markets":
        env["ALL_NO_ENABLED"] = "true"
    # KALSHI_PRIVATE_KEY may be PEM text (Railway-style); materialize a file.
    try:
        from src.utils.mode import _materialize_private_key

        key_path = _materialize_private_key()
        if key_path:
            env["KALSHI_PRIVATE_KEY_PATH"] = key_path
    except Exception:
        pass
    return env


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def row_alive(row) -> bool:
    """Strict: pid exists AND kernel start-time matches the recorded row,
    so a recycled pid can never read as a live lane. Instance stamps are
    deliberately NOT checked here: a keep-alive daemon that restarts must
    still recognise the lanes it spawned before the restart as its own."""
    from src.utils.strategy_runtime import process_start_ticks

    pid = row.get("pid")
    if not pid:
        return False
    if not pid_alive(pid):
        return False
    recorded = row.get("start_ticks")
    if recorded is None:
        return False
    current = process_start_ticks(int(pid))
    return current is not None and int(current) == int(recorded)


async def main() -> None:
    from src.utils.mode import TradingMode
    from src.utils.strategy_runtime import StrategyRuntime, key as rt_key

    apply = "--dry-run" not in sys.argv
    spawn = "--no-spawn" not in sys.argv
    mode_mgr = TradingMode(DB_PATH)
    store = StrategyRuntime(DB_PATH)

    before_mode = await mode_mgr.current()
    before = await store.snapshot()
    print(f"book mode (before): {before_mode}")
    for lane in LANES:
        row = before.get(rt_key(lane, "paper")) or {}
        alive = pid_alive(row.get("pid")) if row.get("pid") else False
        print(
            f"  {lane:22s} desired={row.get('desired')} pid={row.get('pid')} "
            f"alive={alive} stop_reason={row.get('stop_reason')!r}"
        )
    for lane in sorted(LOCKED_OFF):
        row = before.get(rt_key(lane, "paper")) or {}
        print(f"  {lane:22s} [locked off by project law, skipped]")

    if not apply:
        print("dry-run: no changes written")
        return

    new_mode = await mode_mgr.set("dry")
    print(f"book mode -> {new_mode}")

    for lane in LANES:
        await store.arm(lane, "paper")
    print("armed all DRY lanes (desired=1, paper)")

    # Initialize the ALL NO MARKETS book if present in LANES
    if "all_no_markets" in LANES:
        print("🔄 Initializing ALL NO MARKETS book ($250 starting balance)...")
        try:
            from src.strategies.all_no import AllNoKalshiBook
            all_no_book = AllNoKalshiBook(DB_PATH)
            await all_no_book.ensure_schema()
            await all_no_book.seed_book()
            print("✅ ALL NO MARKETS book initialized successfully")
        except Exception as exc:
            print(f"⚠️  ALL NO MARKETS book initialization failed: {type(exc).__name__}: {exc}")
            print("   The strategy may still start, but the book will need to be created manually.")

    if not spawn:
        print("--no-spawn: lanes armed; they resume on next dashboard boot")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    spawned = {}
    for lane, args in LANES.items():
        before_row = (await store.snapshot()).get(rt_key(lane, "paper")) or {}
        if row_alive(before_row):
            print(f"  {lane:22s} already running (pid {before_row['pid']}), skipped")
            continue
        cmd = [sys.executable, "cli.py", *args]
        out = open(LOG_DIR / f"strategy_{lane}.log", "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=child_env(lane),
                start_new_session=True,
            )
        finally:
            out.close()
        # Persist BEFORE reporting, retrying briefly; an untracked process is
        # worse than no process (same rule as _spawn_strategy).
        last_err = None
        for attempt in range(8):
            try:
                await store.record_start(lane, proc.pid, "paper", " ".join(cmd[1:]), desired=True)
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
            print(f"  {lane:22s} FAILED to record ({last_err}); child stopped")
            continue
        spawned[lane] = proc.pid
        print(f"  {lane:22s} spawned (pid {proc.pid})")

    print(f"spawned {len(spawned)}/{len(LANES)} lanes; left off: {', '.join(sorted(LOCKED_OFF))}")

    # Let them boot, then report who is still alive.
    time.sleep(40)
    print("--- liveness after 40s ---")
    snap = await store.snapshot()
    for lane in LANES:
        row = snap.get(rt_key(lane, "paper")) or {}
        alive = row_alive(row)
        hb = row.get("heartbeat_at") or "-"
        print(f"  {lane:22s} pid={row.get('pid')} alive={alive} heartbeat={hb}")
    print(
        "NOTE: these processes run detached from this script. The dashboard's "
        "strategy supervisor is the resilience layer (respawns after crashes); "
        "if you boot the dashboard on this host, reconcile lanes via its Stop "
        "buttons to avoid double processes."
    )


if __name__ == "__main__":
    asyncio.run(main())
