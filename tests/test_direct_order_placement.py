#!/usr/bin/env python3
"""
Direct Order Placement Test
Bypasses AI evaluation and directly tests real order placement on Kalshi.
This ensures we can place REAL orders 100% of the time.
"""

import asyncio
import uuid
import logging
import sys
import os
from datetime import datetime

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.clients.kalshi_client import KalshiClient
from src.utils.database import DatabaseManager, Position
from src.utils.logging_setup import setup_logging
from src.jobs.execute import execute_position

# live: PLACES REAL ORDERS on Kalshi (see tests/conftest.py)
pytestmark = pytest.mark.live


async def test_direct_order_placement():
    """Test direct order placement on a real tradeable market."""

    setup_logging()
    logger = logging.getLogger("direct_order_test")

    logger.info("🎯 Testing DIRECT real order placement (no AI, no simulation)")

    kalshi_client = KalshiClient()
    db_manager = DatabaseManager()

    try:
        await db_manager.initialize()

        # 1. Find a tradeable market
        logger.info("🔍 Finding tradeable markets...")
        markets_response = await kalshi_client.get_markets(limit=200, status="open")
        markets = markets_response.get("markets", [])

        # Find markets with real liquidity
        tradeable_markets = []
        for market in markets:
            yes_ask = market.get("yes_ask", 0)
            no_ask = market.get("no_ask", 0)
            volume = market.get("volume", 0)

            if yes_ask > 0 and yes_ask < 100 and no_ask > 0 and no_ask < 100 and volume > 0:
                tradeable_markets.append(market)

        if not tradeable_markets:
            logger.error("❌ No tradeable markets found")
            return False

        # Use highest volume market
        test_market = max(tradeable_markets, key=lambda m: m.get("volume", 0))
        ticker = test_market["ticker"]
        yes_ask = test_market["yes_ask"]
        no_ask = test_market["no_ask"]
        volume = test_market["volume"]

        logger.info(f"📈 Using highest volume market: {ticker}")
        logger.info(f"   Volume: {volume:,} contracts")
        logger.info(f"   Prices: YES={yes_ask}¢, NO={no_ask}¢")

        # 2. Check current account balance
        balance = await kalshi_client.get_balance()
        available = balance.get("balance", 0)
        logger.info(f"💰 Available balance: ${available/100:.2f}")

        # 3. Check initial positions
        initial_positions = await kalshi_client.get_positions()
        initial_position = 0
        for pos in initial_positions.get("market_positions", []):
            if pos.get("ticker") == ticker:
                initial_position = pos.get("position", 0)
                break

        logger.info(f"📊 Initial position in {ticker}: {initial_position} contracts")

        # 4. Create a small test position (choose the cheaper side)
        if yes_ask <= no_ask:
            side = "YES"
            price_cents = yes_ask
        else:
            side = "NO"
            price_cents = no_ask

        quantity = 1  # Just 1 contract for testing
        cost_cents = price_cents * quantity
        cost_dollars = cost_cents / 100

        logger.info(
            f"💰 Test order: {quantity} {side} contract at {price_cents}¢ = ${cost_dollars:.2f}"
        )

        if cost_cents > available:
            logger.error(f"❌ Insufficient funds: Need {cost_cents}¢, have {available}¢")
            return False

        # 5. Create Position object
        position = Position(
            market_id=ticker,
            side=side,
            quantity=quantity,
            entry_price=price_cents / 100,  # Convert to dollars
            live=False,  # Will be set to True after successful execution
            timestamp=datetime.now(),
            rationale=f"DIRECT ORDER TEST: {side} {quantity} at {price_cents}¢",
            strategy="test_direct_order",
        )

        # 6. Add to database
        position_id = await db_manager.add_position(position)
        if position_id is None:
            logger.error(f"❌ Position already exists for {ticker}")
            return False

        position.id = position_id
        logger.info(f"✅ Position added to database with ID: {position_id}")

        # 7. Execute the REAL order
        logger.info(f"🚀 Placing REAL order on Kalshi...")
        success = await execute_position(
            position=position,
            live_mode=True,  # FORCE live mode - NO simulation
            db_manager=db_manager,
            kalshi_client=kalshi_client,
        )

        if not success:
            logger.error("❌ Order execution failed!")
            return False

        logger.info("✅ Order execution returned success!")

        # 8. Wait and check if position appeared
        await asyncio.sleep(3)

        logger.info("🔍 Checking if order was actually filled...")
        final_positions = await kalshi_client.get_positions()
        final_position = 0
        for pos in final_positions.get("market_positions", []):
            if pos.get("ticker") == ticker:
                final_position = pos.get("position", 0)
                break

        # 9. Check database position status
        import aiosqlite

        async with aiosqlite.connect(db_manager.db_path) as db:
            cursor = await db.execute(
                "SELECT live, status FROM positions WHERE id = ?", (position_id,)
            )
            result = await cursor.fetchone()
            if result:
                db_live, db_status = result
                logger.info(f"💾 Database position: live={db_live}, status={db_status}")
            else:
                logger.error("❌ Position not found in database!")
                return False

        # 10. Verify the order was actually placed
        position_change = final_position - initial_position

        if position_change != 0:
            logger.info(f"🎉 SUCCESS! Position changed by {position_change} contracts")
            logger.info(f"   Before: {initial_position}, After: {final_position}")
            logger.info(f"✅ REAL ORDER WAS PLACED AND FILLED!")
            return True
        else:
            logger.error(f"❌ NO position change detected!")
            logger.error(f"   Database shows success but Kalshi position unchanged")
            logger.error(f"   This suggests order was not actually placed")
            return False

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        await kalshi_client.close()


if __name__ == "__main__":
    result = asyncio.run(test_direct_order_placement())
    if result:
        print("🎉 DIRECT ORDER TEST PASSED! Real orders are being placed on Kalshi.")
    else:
        print("💥 DIRECT ORDER TEST FAILED! Orders are still not being placed properly.")
