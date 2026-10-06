"""Strategy bootstrap validation: kills process if book mode is wrong.

Every strategy process must call verify_book_mode_at_startup() before entering
its trading loop. This enforces IRON-CLAD separation between DRY (paper) and
LIVE (real money) modes:

- If the recorded mode doesn't match the persisted book mode, the process dies
  immediately with exit code 1.
- This prevents the critical bug where restarting a strategy in the wrong book
  could arm LIVE strategies behind a DRY button push.

Root causes killed:
1. Stale book mode read during respawn
2. Strategy process doesn't verify its book mode at runtime
3. Environment variables inherit from previous process
4. Desired state isn't scoped correctly for respawns
5. No heartbeat book mode validation
"""
import asyncio
import logging
import os
import sys
from typing import Optional

from src.utils.logging_setup import get_trading_logger

logger = get_trading_logger(__name__)


async def verify_book_mode_at_startup(
    strategy_name: str,
    expected_mode: Optional[str] = None,
) -> str:
    """CRITICAL: Called by EVERY strategy process at startup. Dies if mode is wrong.

    Args:
        strategy_name: Name of the strategy (e.g. 'ai_directional', 'quick_flip')
        expected_mode: The mode this process should run in ('paper' or 'live').
                      If not provided, reads from environment.

    Returns:
        The verified mode ('paper' or 'live').

    Raises:
        SystemExit: Exits with code 1 if modes don't match.
    """
    from src.utils.mode import TradingMode
    from src.utils.strategy_runtime import StrategyRuntime, key as rt_key

    db_path = os.getenv("DB_PATH", "trading_system.db")
    mode_mgr = TradingMode(db_path)
    runtime_store = StrategyRuntime(db_path)

    # Read the PERSISTED book mode from the database (source of truth)
    try:
        persisted_book = await asyncio.wait_for(mode_mgr.current(), timeout=10.0)
    except Exception as e:
        logger.critical(f"Failed to read persisted book mode at startup: {e}")
        sys.exit(1)

    # Read THIS PROCESS's recorded mode from the strategy_runtime table
    try:
        runtime_snapshot = await asyncio.wait_for(runtime_store.snapshot(), timeout=10.0)
    except Exception as e:
        logger.critical(f"Failed to read strategy runtime snapshot at startup: {e}")
        sys.exit(1)

    # Get the composite key for this strategy
    if expected_mode is None:
        # Try to infer from environment if not provided
        expected_mode = os.getenv("STRATEGY_BOOK_MODE")
        if not expected_mode:
            # Fall back to reading from the runtime table
            for mode in ("paper", "live"):
                this_key = rt_key(strategy_name, mode)
                if this_key in runtime_snapshot:
                    row = runtime_snapshot[this_key]
                    if row.get("desired") or row.get("pid"):
                        expected_mode = mode
                        break

    if not expected_mode:
        logger.critical(
            f"Cannot determine expected mode for {strategy_name} at startup. "
            f"No mode in environment, no desired row in database. Killing process."
        )
        sys.exit(1)

    # Map book mode to runtime mode for comparison
    # "dry" → "paper", "live" → "live"
    runtime_persisted_mode = "live" if persisted_book == "live" else "paper"

    # HARD ASSERTION: The persisted book mode MUST match this process's recorded mode
    if runtime_persisted_mode != expected_mode:
        logger.critical(
            f"BOOK MODE MISMATCH AT STARTUP: "
            f"persisted_book={persisted_book} (maps to {runtime_persisted_mode}), "
            f"expected_mode={expected_mode}, strategy={strategy_name}. "
            f"This is the DRY/LIVE cross-contamination bug. Killing process immediately."
        )
        sys.exit(1)

    logger.info(f"Book mode verified at startup: {persisted_book} ({expected_mode}) - SAFE")
    return expected_mode


async def start_heartbeat_validator(
    strategy_name: str,
    expected_mode: str,
    check_interval_sec: float = 30.0,
) -> None:
    """Heartbeat loop that validates book mode hasn't changed under us.

    Runs in background. Every 30s (by default), verifies the book hasn't switched.
    If it has, the process kills itself immediately.

    Args:
        strategy_name: Name of the strategy
        expected_mode: The mode this process should run in ('paper' or 'live')
        check_interval_sec: Seconds between checks
    """
    from src.utils.mode import TradingMode
    from src.utils.strategy_runtime import StrategyRuntime

    db_path = os.getenv("DB_PATH", "trading_system.db")
    mode_mgr = TradingMode(db_path)
    runtime_store = StrategyRuntime(db_path)

    while True:
        try:
            # Read current book mode from database
            current_book = await asyncio.wait_for(mode_mgr.current(), timeout=10.0)
            current_mode = "live" if current_book == "live" else "paper"

            # HARD GATE: If modes diverge, PANIC and kill
            if current_mode != expected_mode:
                logger.critical(
                    f"HEARTBEAT DETECTED BOOK MODE CHANGE: "
                    f"current={current_book}→{current_mode}, expected={expected_mode}. "
                    f"Someone switched the book while we were trading. KILLING PROCESS."
                )
                sys.exit(1)

            # Heartbeat OK; stamp it
            await asyncio.wait_for(
                runtime_store.record_heartbeat(strategy_name, expected_mode),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error(f"Heartbeat validation timed out for {strategy_name}")
        except Exception as e:
            logger.error(f"Heartbeat validation failed for {strategy_name}: {e}")

        await asyncio.sleep(check_interval_sec)
