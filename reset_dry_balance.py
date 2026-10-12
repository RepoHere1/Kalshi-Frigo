#!/usr/bin/env python3
"""
Script to reset DRY balance to $250
"""

import asyncio
import os
import sys
from pathlib import Path

# Add the project root to the path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.utils.mode import TradingMode

async def reset_dry_balance():
    """Reset DRY balance to $250"""
    db_path = os.getenv("DB_PATH", "trading_system.db")
    mode = TradingMode(db_path)
    
    print("Resetting DRY balance to $250...")
    try:
        # First, reset the entire DRY account to start fresh
        result = await mode.reset_dry_account()
        print(f"Reset completed. Starting balance: ${result['starting_balance']}")
        
        # Then explicitly set the cash to $250
        await mode.set_dry_cash(250.0)
        updated_result = await mode.dry_account()
        print(f"Updated cash balance: ${updated_result['cash']}")
        print("DRY balance has been reset to $250")
        
    except Exception as e:
        print(f"Error resetting DRY balance: {e}")
        return False
    
    return True

if __name__ == "__main__":
    asyncio.run(reset_dry_balance())