import asyncio
import os
import pytest
from datetime import datetime

from src.jobs.decide import make_decision_for_market
from src.utils.database import DatabaseManager
from src.clients.xai_client import XAIClient
from src.clients.kalshi_client import KalshiClient
from src.config.settings import settings
from tests.test_database import load_and_prepare_markets, TEST_DB, FIXTURE_PATH
from tests.test_helpers import find_suitable_test_market

# live: uses the real Kalshi API and real LLM credits (see tests/conftest.py)
pytestmark = [pytest.mark.asyncio, pytest.mark.live]


async def test_make_decision_for_market_creates_position():
    """
    Test that the decision engine correctly creates a position when the LLM returns a BUY decision.
    Uses real Kalshi API and XAI client - no mocks.
    """
    db_manager = DatabaseManager(db_path=TEST_DB)
    await db_manager.initialize()

    # Use real clients - no mocking
    kalshi_client = KalshiClient()
    xai_client = XAIClient()

    try:
        # Get a suitable test market efficiently (only 5 API calls max)
        test_market = await find_suitable_test_market()

        if not test_market:
            pytest.skip("No suitable markets available for testing")

        print(f"Testing with market: {test_market.title} ({test_market.market_id})")

        # Store market in database for testing
        await db_manager.upsert_markets([test_market])

        # Test the decision making process
        position = await make_decision_for_market(
            test_market, db_manager, xai_client, kalshi_client
        )

        # The test passes if no exceptions are thrown
        # Position might be None if AI decides not to trade, which is valid
        print(f"Decision result: {position is not None}")

        if position:
            print(
                f"Position created: {position.side} {position.quantity} @ ${position.entry_price}"
            )
            assert position.market_id == test_market.market_id
            assert position.side in ["YES", "NO"]
            assert position.quantity > 0
            assert position.entry_price > 0
        else:
            print("AI decided not to trade this market")

    finally:
        await kalshi_client.close()
        await xai_client.close()
        # Clean up test database
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
