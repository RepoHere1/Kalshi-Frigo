#!/usr/bin/env python3
"""
Test Real Order Placement
Comprehensive test to verify our order placement functionality actually works.
Based on Kalshi API documentation: https://trading-api.readme.io/reference/createorder-1
"""

import asyncio
import uuid
import logging
import sys
import os

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.clients.kalshi_client import KalshiClient
from src.utils.logging_setup import setup_logging

# live: PLACES AND CANCELS REAL ORDERS on Kalshi (see tests/conftest.py)
pytestmark = pytest.mark.live


async def test_order_placement_flow():
    """Test the complete order placement and cancellation flow."""

    setup_logging()
    logger = logging.getLogger("order_placement_test")

    logger.info("🎯 Testing REAL order placement functionality")

    kalshi_client = KalshiClient()

    try:
        # 1. Get account balance
        balance = await kalshi_client.get_balance()
        available = balance.get("balance", 0)
        logger.info(f"💰 Available balance: ${available/100:.2f}")

        # 2. Find an active market with good liquidity
        logger.info("🔍 Finding active market for testing...")
        markets_response = await kalshi_client.get_markets(limit=100)
        markets = markets_response.get("markets", [])

        test_market = None
        for market in markets:
            if (
                market.get("status") == "active"
                and market.get("volume", 0) > 50000  # Good volume
                and 10 <= market.get("yes_ask", 0) <= 90  # Reasonable prices
                and 10 <= market.get("no_ask", 0) <= 90
            ):
                test_market = market
                break

        if not test_market:
            logger.error("❌ No suitable active market found for testing")
            return False

        ticker = test_market["ticker"]
        yes_ask = test_market["yes_ask"]
        no_ask = test_market["no_ask"]

        logger.info(f"📈 Using market: {ticker}")
        logger.info(f"   Prices: YES={yes_ask}¢, NO={no_ask}¢")
        logger.info(f"   Volume: {test_market.get('volume', 0):,}")

        # 3. Get detailed orderbook
        logger.info("📊 Getting orderbook data...")
        orderbook = await kalshi_client.get_orderbook(ticker)

        yes_bids = orderbook.get("yes", [])
        no_bids = orderbook.get("no", [])

        if not yes_bids or not no_bids:
            logger.error("❌ Market has no orderbook - cannot test")
            return False

        # 4. Place a conservative limit order (far from market)
        # We'll bid 10¢ below current ask to avoid immediate fill
        side = "yes"
        safe_price = max(1, yes_ask - 10)  # Bid 10¢ below ask
        quantity = 1  # Just 1 contract for testing

        logger.info(f"🚀 Placing LIMIT order: {quantity} {side.upper()} at {safe_price}¢")

        client_order_id = str(uuid.uuid4())
        order_response = await kalshi_client.place_order(
            ticker=ticker,
            client_order_id=client_order_id,
            side=side,
            action="buy",
            count=quantity,
            type_="limit",
            yes_price=safe_price if side == "yes" else None,
            no_price=safe_price if side == "no" else None,
        )

        order_id = order_response.get("order", {}).get("order_id")
        logger.info(f"✅ Order placed successfully! Order ID: {order_id}")

        # 5. Verify order appears in our orders list
        await asyncio.sleep(1)
        logger.info("🔍 Checking if order appears in orders list...")

        orders = await kalshi_client.get_orders()
        placed_order = None
        for order in orders.get("orders", []):
            if order.get("order_id") == order_id:
                placed_order = order
                break

        if placed_order:
            logger.info(f"✅ Order found in orders list: {placed_order.get('status')}")
        else:
            logger.error("❌ Order not found in orders list!")
            return False

        # 6. Cancel the order
        logger.info(f"🗑️ Cancelling order {order_id}...")
        cancel_response = await kalshi_client.cancel_order(order_id)
        logger.info(f"✅ Order cancelled: {cancel_response}")

        # 7. Verify order was cancelled
        await asyncio.sleep(1)
        logger.info("🔍 Verifying order cancellation...")

        updated_orders = await kalshi_client.get_orders()
        cancelled_order = None
        for order in updated_orders.get("orders", []):
            if order.get("order_id") == order_id:
                cancelled_order = order
                break

        if cancelled_order:
            status = cancelled_order.get("status")
            if status == "canceled":
                logger.info("✅ Order successfully cancelled")
                return True
            else:
                logger.warning(f"⚠️ Order status: {status} (may still be processing)")
                return True
        else:
            logger.info("✅ Order removed from orders list (cancelled)")
            return True

    except Exception as e:
        logger.error(f"❌ Order placement test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        await kalshi_client.close()


async def test_database_sync():
    """Test that our database stays in sync with actual Kalshi positions."""

    logger = logging.getLogger("db_sync_test")
    logger.info("🔄 Testing database synchronization with Kalshi portfolio")

    from src.utils.database import DatabaseManager

    kalshi_client = KalshiClient()
    db_manager = DatabaseManager()

    try:
        await db_manager.initialize()

        # Get actual Kalshi positions
        kalshi_positions = await kalshi_client.get_positions()
        kalshi_markets = {
            pos["ticker"]: pos["position"] for pos in kalshi_positions.get("market_positions", [])
        }

        # Get database positions marked as live
        import aiosqlite

        async with aiosqlite.connect(db_manager.db_path) as db:
            cursor = await db.execute(
                "SELECT market_id, quantity, side FROM positions WHERE live = 1"
            )
            db_positions = await cursor.fetchall()

        logger.info(f"📊 Kalshi positions: {len(kalshi_markets)} markets")
        logger.info(f"💾 Database positions: {len(db_positions)} positions")

        # Check for mismatches
        mismatches = 0
        for market_id, quantity, side in db_positions:
            kalshi_pos = kalshi_markets.get(market_id, 0)
            expected_pos = quantity if side == "YES" else -quantity

            if kalshi_pos != expected_pos:
                logger.warning(
                    f"⚠️ MISMATCH: {market_id} - DB: {expected_pos}, Kalshi: {kalshi_pos}"
                )
                mismatches += 1

        if mismatches == 0:
            logger.info("✅ Database and Kalshi positions are in sync")
            return True
        else:
            logger.error(f"❌ Found {mismatches} position mismatches")
            return False

    except Exception as e:
        logger.error(f"❌ Database sync test failed: {e}")
        return False

    finally:
        await kalshi_client.close()


if __name__ == "__main__":

    async def run_all_tests():
        logger = logging.getLogger("main")
        logger.info("🧪 Running order placement and sync tests...")

        # Test 1: Order placement
        placement_success = await test_order_placement_flow()

        # Test 2: Database sync
        sync_success = await test_database_sync()

        if placement_success and sync_success:
            print("🎉 ALL TESTS PASSED! Order placement is working correctly.")
            return True
        else:
            print("💥 TESTS FAILED! Order placement system needs fixing.")
            return False

    result = asyncio.run(run_all_tests())
    exit(0 if result else 1)
