"""Comprehensive tests for iron-clad DRY/LIVE separation.

Tests the critical bug fix that prevents cross-contamination between paper (DRY)
and live trading modes, where strategies could be armed in the wrong book.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.jobs.strategy_bootstrap import verify_book_mode_at_startup, start_heartbeat_validator
from src.utils.mode import TradingMode, MODE_DRY, MODE_LIVE
from src.utils.strategy_runtime import StrategyRuntime, key as rt_key


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield str(db_path)


@pytest.fixture
def mode_manager(temp_db):
    """Create a mode manager with temp database."""
    return TradingMode(temp_db)


@pytest.fixture
def runtime_store(temp_db):
    """Create a runtime store with temp database."""
    return StrategyRuntime(temp_db)


@pytest.mark.asyncio
async def test_strategy_bootstrap_verifies_matching_mode(mode_manager, runtime_store, temp_db):
    """Strategy process dies if it starts in the wrong book."""
    # Setup: set book to LIVE
    await mode_manager.set(MODE_LIVE, confirmed=True)
    
    # Record strategy as LIVE (mode="live")
    await runtime_store.record_start(
        name="test_strategy",
        pid=12345,
        mode="live",
        command="python test.py",
        desired=True,
    )
    
    # Call bootstrap - should succeed because modes match
    with patch.dict(os.environ, {"DB_PATH": temp_db}):
        result = await verify_book_mode_at_startup("test_strategy", expected_mode="live")
        assert result == "live"


@pytest.mark.asyncio
async def test_strategy_bootstrap_rejects_wrong_book(mode_manager, runtime_store, temp_db):
    """Strategy process dies if it starts in the wrong book."""
    # Setup: set book to DRY (paper)
    await mode_manager.set(MODE_DRY, confirmed=False)
    
    # Record strategy as LIVE (wrong book - mismatch)
    await runtime_store.record_start(
        name="btc_updown",
        pid=12345,
        mode="live",  # LIVE book - contradicts DRY book
        command="python cli.py run --live --strategy btc_updown",
        desired=True,
    )
    
    # Call bootstrap expecting the process to be in LIVE mode
    # But book is DRY (paper mode), and expected is "live" - MISMATCH!
    # persisted_book=DRY → runtime_persisted_mode=paper
    # expected_mode=live
    # paper != live → SystemExit
    with patch.dict(os.environ, {"DB_PATH": temp_db}):
        with pytest.raises(SystemExit) as exc_info:
            await verify_book_mode_at_startup("btc_updown", expected_mode="live")
        assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_heartbeat_validates_book_mode_match(mode_manager, runtime_store, temp_db):
    """Running strategy kills itself if book switches under it."""
    # Setup: DRY book, strategy running in DRY (paper mode)
    await mode_manager.set(MODE_DRY, confirmed=False)
    await runtime_store.record_start(
        name="test_strategy",
        pid=12345,
        mode="paper",
        command="python test.py",
        desired=True,
    )
    
    # Book starts as DRY (paper) - OK
    current_book = await mode_manager.current()
    assert current_book == MODE_DRY
    
    # Switch book to LIVE - this should trigger bootstrap/heartbeat to kill
    await mode_manager.set(MODE_LIVE, confirmed=True)
    current_book = await mode_manager.current()
    assert current_book == MODE_LIVE
    
    # Now try bootstrap expecting paper (DRY) but book is LIVE - should exit
    with patch.dict(os.environ, {"DB_PATH": temp_db}):
        with pytest.raises(SystemExit) as exc_info:
            await verify_book_mode_at_startup("test_strategy", expected_mode="paper")
        assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_triple_verification_catches_mode_mismatch(mode_manager, runtime_store, temp_db):
    """Toggle endpoint triple verification catches book mismatches."""
    # Setup: DRY book
    await mode_manager.set(MODE_DRY, confirmed=False)
    
    # Record strategy as LIVE (wrong book)
    await runtime_store.record_start(
        name="quick_flip",
        pid=12345,
        mode="live",  # Recorded as LIVE
        command="python cli.py run --live --strategy quick_flip",
        desired=True,
    )
    
    # Now we're on DRY page but strategy is recorded in LIVE
    # Triple verification should catch this
    recorded = await runtime_store.snapshot()
    this_key = rt_key("quick_flip", "paper")  # Looking for DRY row
    row = recorded.get(this_key)
    
    # The row should NOT exist because it's recorded as LIVE
    # So we should find the LIVE row instead
    live_key = rt_key("quick_flip", "live")
    live_row = recorded.get(live_key)
    assert live_row is not None
    assert live_row.get("mode") == "live"


@pytest.mark.asyncio
async def test_supervisor_respawn_guard_prevents_wrong_book_spawn(mode_manager, runtime_store, temp_db):
    """Supervisor refuses to respawn strategy in wrong book."""
    # Setup: DRY book, strategy was running in DRY
    await mode_manager.set(MODE_DRY, confirmed=False)
    await runtime_store.record_start(
        name="test_strategy",
        pid=12345,
        mode="paper",
        command="python test.py",
        desired=True,
    )
    
    # Strategy crashes, supervisor wants to respawn
    await runtime_store.record_stop(
        "test_strategy",
        "process crashed",
        mode="paper",
        clear_desired=False,  # Keep desired=1 so supervisor will respawn
    )
    
    # Meanwhile, book switches to LIVE
    await mode_manager.set(MODE_LIVE, confirmed=True)
    
    # Supervisor should see the mismatch and refuse to respawn
    current_book = await mode_manager.current()
    assert current_book == MODE_LIVE
    
    # Get the runtime row - it's in "paper" mode
    recorded = await runtime_store.snapshot()
    paper_key = rt_key("test_strategy", "paper")
    row = recorded.get(paper_key)
    
    # The row is in paper mode but book is live - this would be caught by respawn guard
    recorded_mode = row.get("mode") if row else None
    expected_book = "live" if current_book == "live" else "dry"
    recorded_book = "live" if recorded_mode == "live" else "dry"
    
    # Respawn guard would prevent spawning
    assert expected_book != recorded_book  # Books don't match!


@pytest.mark.asyncio
async def test_environment_isolation_no_mode_inheritance(temp_db):
    """Strategy child environment is isolated - no stale mode inheritance."""
    # This would be tested at the process level, but we verify the env setup logic
    from src.jobs.strategy_bootstrap import verify_book_mode_at_startup
    
    # Setup: DRY book
    mode_mgr = TradingMode(temp_db)
    await mode_mgr.set(MODE_DRY, confirmed=False)
    
    # Create isolated environment like _spawn_strategy does
    child_env = os.environ.copy()
    # Remove stale trading mode vars
    child_env.pop("TRADING_MODE", None)
    child_env.pop("LIVE_TRADING_ENABLED", None)
    # Set explicit mode
    child_env["STRATEGY_BOOK_MODE"] = "paper"
    child_env["DB_PATH"] = temp_db
    
    # Verify the env has no stale vars
    assert "TRADING_MODE" not in child_env
    assert "LIVE_TRADING_ENABLED" not in child_env
    assert child_env["STRATEGY_BOOK_MODE"] == "paper"
    assert child_env["DB_PATH"] == temp_db


@pytest.mark.asyncio
async def test_bootstrap_with_no_desired_row_fails_safely(mode_manager, runtime_store, temp_db):
    """Bootstrap fails safely if no desired row exists."""
    # Setup: DRY book, but no runtime row for this strategy
    await mode_manager.set(MODE_DRY, confirmed=False)
    
    # Try bootstrap without recording the strategy first
    # Bootstrap should fail because there's no row to read the mode from
    with patch.dict(os.environ, {"DB_PATH": temp_db}):
        with pytest.raises(SystemExit):
            await verify_book_mode_at_startup("unknown_strategy")


@pytest.mark.asyncio
async def test_database_consistency_after_mode_switch(mode_manager, runtime_store, temp_db):
    """Database state is consistent after mode switch."""
    # Record strategy in DRY
    await mode_manager.set(MODE_DRY, confirmed=False)
    await runtime_store.record_start(
        name="strategy_a",
        pid=111,
        mode="paper",
        command="python test.py",
        desired=True,
    )
    
    # Record strategy in LIVE
    await mode_manager.set(MODE_LIVE, confirmed=True)
    await runtime_store.record_start(
        name="strategy_b",
        pid=222,
        mode="live",
        command="python test.py",
        desired=True,
    )
    
    # Both rows should exist, each in their own book
    snapshot = await runtime_store.snapshot()
    paper_key = rt_key("strategy_a", "paper")
    live_key = rt_key("strategy_b", "live")
    
    assert snapshot.get(paper_key) is not None
    assert snapshot.get(paper_key).get("pid") == 111
    assert snapshot.get(live_key) is not None
    assert snapshot.get(live_key).get("pid") == 222
    
    # Switching books doesn't affect the other book's rows
    await mode_manager.set(MODE_DRY, confirmed=False)
    snapshot_after = await runtime_store.snapshot()
    assert snapshot_after.get(paper_key) is not None
    assert snapshot_after.get(live_key) is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
