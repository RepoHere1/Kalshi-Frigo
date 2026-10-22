#!/usr/bin/env python3
"""
Test Immediate Trading Fix
Verify our fix for immediate trading actually places real orders on Kalshi.
"""

import asyncio
import logging
import sys
import os
from datetime import datetime

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.utils.database import DatabaseManager, Market
from src.utils.logging_setup import setup_logging
from src.strategies.portfolio_optimization import create_market_opportunities_from_markets

# live: PLACES REAL ORDERS on Kalshi (see tests/conftest.py)
pytestmark = pytest.mark.live


async def test_immediate_trading_fix():
    """Test if immediate trading fix actually places real orders."""

    setup_logging()
    logger = logging.getLogger("immediate_fix_test")

    logger.info("🎯 Testing Immediate Trading Fix")

    # Initialize clients
    kalshi_client = KalshiClient()
    xai_client = XAIClient()
    db_manager = DatabaseManager()

    try:
        await db_manager.initialize()

        # 1. Check initial Kalshi positions
        logger.info("📊 Checking initial Kalshi positions...")
        initial_positions = await kalshi_client.get_positions()
        initial_markets = {
            pos["ticker"]: pos["position"] for pos in initial_positions.get("market_positions", [])
        }
        logger.info(f"Initial positions in {len(initial_markets)} markets")

        # 2. Find ACTUALLY TRADEABLE markets
        logger.info("🔍 Finding TRADEABLE markets (not just any markets)...")

        # Get markets with more specific criteria for active trading
        markets_response = await kalshi_client.get_markets(
            limit=200,  # Get more markets to find tradeable ones
            status="open",  # Only get open markets
        )
        markets = markets_response.get("markets", [])

        logger.info(f"Found {len(markets)} OPEN markets")

        # Find tradeable markets with real bid/ask spreads
        tradeable_markets = []
        for market in markets:
            yes_ask = market.get("yes_ask", 0)
            no_ask = market.get("no_ask", 0)
            yes_bid = market.get("yes_bid", 0)
            no_bid = market.get("no_bid", 0)
            volume = market.get("volume", 0)

            # Market is tradeable if it has real prices and some activity
            if (
                yes_ask > 0 and yes_ask < 100 and no_ask > 0 and no_ask < 100 and volume > 0
            ):  # Has some trading activity
                tradeable_markets.append(market)

        logger.info(f"Found {len(tradeable_markets)} TRADEABLE markets")

        # Show top tradeable markets
        for i, market in enumerate(tradeable_markets[:5]):
            ticker = market.get("ticker", "Unknown")
            volume = market.get("volume", 0)
            yes_ask = market.get("yes_ask", 0)
            no_ask = market.get("no_ask", 0)
            logger.info(f"   {i+1}. {ticker}: vol={volume:,}, YES={yes_ask}¢, NO={no_ask}¢")

        if not tradeable_markets:
            logger.error(
                "❌ No tradeable markets found - all markets may be closed or have no liquidity"
            )
            logger.info("💡 This is expected during off-hours or when markets are closed")
            return False

        # Use the first tradeable market
        test_market_data = tradeable_markets[0]

        ticker = test_market_data["ticker"]
        logger.info(f"📈 Using test market: {ticker}")
        logger.info(f"   Volume: {test_market_data.get('volume', 0):,}")
        logger.info(
            f"   Prices: YES={test_market_data.get('yes_ask')}¢, NO={test_market_data.get('no_ask')}¢"
        )

        # 3. Create Market object for immediate trading test
        market = Market(
            market_id=ticker,
            title=test_market_data.get("title", "Test Market"),
            yes_price=test_market_data.get("yes_ask", 50),
            no_price=test_market_data.get("no_ask", 50),
            volume=test_market_data.get("volume", 0),
            expiration_ts=test_market_data.get("close_ts", int(datetime.now().timestamp()) + 86400),
            category=test_market_data.get("category", "other"),
            status=test_market_data.get("status", "open"),
            last_updated=datetime.now(),
            has_position=False,
        )

        # 4. Test immediate trading by creating opportunities
        logger.info("🚀 Testing immediate trading opportunity creation...")
        opportunities = await create_market_opportunities_from_markets(
            [market], xai_client, kalshi_client, db_manager, 1000  # $1000 test capital
        )

        logger.info(f"Created {len(opportunities)} opportunities")
        for opp in opportunities:
            logger.info(f"   {opp.market_id}: Edge={opp.edge:.1%}, Confidence={opp.confidence:.1%}")

        # 5. Wait a moment for any async execution
        await asyncio.sleep(3)

        # 6. Check database for new positions
        logger.info("💾 Checking database for new positions...")
        import aiosqlite

        async with aiosqlite.connect(db_manager.db_path) as db:
            cursor = await db.execute(
                "SELECT market_id, side, quantity, live, status, rationale FROM positions WHERE market_id = ?",
                (ticker,),
            )
            db_positions = await cursor.fetchall()

        if db_positions:
            logger.info(f"✅ Found {len(db_positions)} positions in database for {ticker}")
            for pos in db_positions:
                logger.info(
                    f"   {pos[0]}: {pos[1]} {pos[2]} shares, live={pos[3]}, status={pos[4]}"
                )
        else:
            logger.info(f"⚠️ No positions found in database for {ticker}")

        # 7. Check Kalshi for new positions
        logger.info("📊 Checking Kalshi for new positions...")
        final_positions = await kalshi_client.get_positions()
        final_markets = {
            pos["ticker"]: pos["position"] for pos in final_positions.get("market_positions", [])
        }

        # Check if our test market has a position now
        new_position = final_markets.get(ticker, 0)
        initial_position = initial_markets.get(ticker, 0)

        if new_position != initial_position:
            logger.info(
                f"✅ SUCCESS! New position in {ticker}: {new_position} contracts (was {initial_position})"
            )
            return True
        else:
            logger.error(f"❌ NO position change in {ticker} on Kalshi (still {initial_position})")

            # If database has position but Kalshi doesn't, it means our execution failed
            if db_positions:
                logger.error(
                    "🚨 CRITICAL: Database has position but Kalshi doesn't - orders not actually placed!"
                )

            return False

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        await kalshi_client.close()


if __name__ == "__main__":
    result = asyncio.run(test_immediate_trading_fix())
    if result:
        print("🎉 IMMEDIATE TRADING FIX SUCCESSFUL! Real orders are being placed.")
    else:
        print("💥 IMMEDIATE TRADING FIX FAILED! Orders still not being placed properly.")
