#!/usr/bin/env python3
"""
Railway pre-deployment DRY lane arming script.

This script arms all DRY lanes BEFORE Railway deployment to ensure
that DRY lanes stay ON after redeploy.

It should be run BEFORE deploying to Railway:
1. Set book mode to 'dry' (persists across redeploys)
2. Arm all DRY lanes (set desired=1 for each lane)
3. Clear any stop reasons on the lanes

After deployment, when the dashboard supervisor starts, it will
manage these armed lanes, keeping them ON permanently.
"""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

from src.utils.mode import TradingMode
from src.utils.strategy_runtime import StrategyRuntime, key as rt_key

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = str(BASE_DIR / "trading_system.db")

def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)

async def arm_dry_lanes(emergency: bool = False) -> None:
    """Arm all DRY lanes before deployment."""
    from turn_on_dry_lanes import LANES
    
    store = StrategyRuntime(DB_PATH)
    mode_mgr = TradingMode(DB_PATH)
    
    log("Starting DRY lane pre-deployment arming...")
    
    # Set book mode to 'dry' if not already set
    before_mode = await mode_mgr.current()
    new_mode = await mode_mgr.set("dry")
    log(f"Book mode: {before_mode} → {new_mode}")
    
    # Arm all DRY lanes
    armed_count = 0
    for lane in LANES:
        try:
            # Check current state
            before_row = (await store.snapshot()).get(rt_key(lane, "paper")) or {}
            before_desired = int(before_row.get("desired") or 0)
            before_stop_reason = before_row.get("stop_reason")
            
            # Arm the lane
            await store.arm(lane, "paper")
            
            after_row = (await store.snapshot()).get(rt_key(lane, "paper")) or {}
            after_desired = int(after_row.get("desired") or 0)
            after_stop_reason = after_row.get("stop_reason")
            
            armed_count += 1
            log(f"  {lane:22s} desired {before_desired} → {after_desired} "
                f"stop_reason {before_stop_reason!r} → {after_stop_reason!r}")
                
        except Exception as exc:
            log(f"  {lane:22s} ERROR: {type(exc).__name__}: {exc}")
    
    log(f"Armed {armed_count}/{len(LANES)} DRY lanes")
    
    if emergency:
        log("⚠️  Emergency arming - dashboard may be running")
    else:
        log("✓ Pre-deployment arming complete")
        log("✓ DRY lanes will stay ON after Railway deployment")

async def main() -> None:
    emergency = "--emergency" in sys.argv
    await arm_dry_lanes(emergency=emergency)

if __name__ == "__main__":
    asyncio.run(main())
