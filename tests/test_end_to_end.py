import asyncio
import os
import pytest
from datetime import datetime

from src.jobs.decide import make_decision_for_market
from src.jobs.execute import execute_position
from src.jobs.ingest import run_ingestion
from src.utils.database import DatabaseManager, Market
from src.clients.xai_client import XAIClient
from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from tests.test_helpers import find_suitable_test_market

# live: uses the real Kalshi API and real LLM credits (see tests/conftest.py)
pytestmark = [pytest.mark.asyncio, pytest.mark.live]

E2E_TEST_DB = "e2e_test_trading_system.db"


async def test_full_trading_cycle():
    """
    Test the complete trading cycle: ingest -> decide -> execute.
    Uses real Kalshi API and XAI client - OPTIMIZED to reduce API calls.
    """
    # Initialize real clients - no mocking
    db_manager = DatabaseManager(db_path=E2E_TEST_DB)
    await db_manager.initialize()

    kalshi_client = KalshiClient()
    xai_client = XAIClient()

    try:
        # Step 1: Get a single test market efficiently (no ingestion of all markets)
        test_market = await find_suitable_test_market()

        if not test_market:
            pytest.skip("No suitable markets available for end-to-end testing")

        print(f"End-to-end test with market: {test_market.title} ({test_market.market_id})")

        # Store the test market in database
        await db_manager.upsert_markets([test_market])
        print(f"✅ Market stored in database")

        # Step 2: Decision making with real AI
        print("🤖 Testing decision making process...")
        position = await make_decision_for_market(
            test_market, db_manager, xai_client, kalshi_client
        )

        if position:
            print(
                f"✅ Decision made: {position.side} {position.quantity} @ ${position.entry_price}"
            )

            # Step 3: Execute position (this creates the position in database)
            print("🚀 Testing position execution...")
            execution_result = await execute_position(position, kalshi_client)

            if execution_result:
                print(f"✅ Position executed successfully")

                # Verify position was stored
                stored_positions = await db_manager.get_open_positions()
                assert len(stored_positions) > 0, "Position should be stored in database"

                stored_position = stored_positions[0]
                assert stored_position.market_id == test_market.market_id
                print(f"✅ Position verified in database: {stored_position.market_id}")
            else:
                print(
                    "📊 Position execution returned False (may be intentional based on market conditions)"
                )
        else:
            print("📊 AI decided not to trade this market (valid outcome)")

        print("✅ End-to-end test completed successfully")

    finally:
        await kalshi_client.close()
        await xai_client.close()
        # Clean up test database
        if os.path.exists(E2E_TEST_DB):
            os.remove(E2E_TEST_DB)
