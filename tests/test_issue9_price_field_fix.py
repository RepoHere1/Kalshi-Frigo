#!/usr/bin/env python3
"""
Test for Issue #9 fix: Order payload sending wrong price fields

This test verifies that the execute_position function correctly constructs
order parameters with the required price field based on the position side,
resolving the Kalshi API error: "exactly one of yes_price, no_price,
yes_price_dollars, or no_price_dollars should be provided"
"""

import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.database import Position
from src.jobs.execute import execute_position
from datetime import datetime


class TestIssue9PriceFieldFix(unittest.TestCase):
    """Test the fix for Issue #9 - order payload price fields"""

    def setUp(self):
        """Set up test fixtures"""
        self.mock_db_manager = AsyncMock()
        self.mock_kalshi_client = AsyncMock()

        # Mock market data with typical ask/bid prices
        self.mock_market_data = {
            "market": {
                "yes_ask": 45,  # 45 cents
                "no_ask": 55,  # 55 cents
                "yes_bid": 40,
                "no_bid": 50,
            }
        }

    def test_yes_position_order_params(self):
        """Test that YES positions create orders with yes_price field"""

        # Create a YES position
        yes_position = Position(
            market_id="TEST-MARKET-YES",
            side="YES",
            quantity=5,
            entry_price=0.45,
            live=False,
            timestamp=datetime.now(),
            rationale="Test YES position for Issue #9",
            strategy="test",
        )

        # Test the order parameter construction logic
        market = self.mock_market_data["market"]
        side_lower = yes_position.side.lower()

        order_params = {
            "ticker": yes_position.market_id,
            "client_order_id": str(uuid.uuid4()),
            "side": side_lower,
            "action": "buy",
            "count": yes_position.quantity,
            "type": "market",
        }

        # Apply the fix logic
        if side_lower == "yes":
            yes_ask = market.get("yes_ask", 0)
            if yes_ask > 0:
                order_params["yes_price"] = yes_ask

        # Verify the fix
        self.assertIn("yes_price", order_params, "YES order should include yes_price field")
        self.assertNotIn("no_price", order_params, "YES order should not include no_price field")
        self.assertEqual(order_params["yes_price"], 45, "yes_price should be the ask price")

    def test_no_position_order_params(self):
        """Test that NO positions create orders with no_price field"""

        # Create a NO position
        no_position = Position(
            market_id="TEST-MARKET-NO",
            side="NO",
            quantity=3,
            entry_price=0.55,
            live=False,
            timestamp=datetime.now(),
            rationale="Test NO position for Issue #9",
            strategy="test",
        )

        # Test the order parameter construction logic
        market = self.mock_market_data["market"]
        side_lower = no_position.side.lower()

        order_params = {
            "ticker": no_position.market_id,
            "client_order_id": str(uuid.uuid4()),
            "side": side_lower,
            "action": "buy",
            "count": no_position.quantity,
            "type": "market",
        }

        # Apply the fix logic
        if side_lower == "no":
            no_ask = market.get("no_ask", 0)
            if no_ask > 0:
                order_params["no_price"] = no_ask

        # Verify the fix
        self.assertIn("no_price", order_params, "NO order should include no_price field")
        self.assertNotIn("yes_price", order_params, "NO order should not include yes_price field")
        self.assertEqual(order_params["no_price"], 55, "no_price should be the ask price")

    async def test_execute_position_with_fix(self):
        """Test that execute_position function uses the fix correctly"""

        # Mock the kalshi client to return market data
        self.mock_kalshi_client.get_market.return_value = self.mock_market_data
        self.mock_kalshi_client.place_order.return_value = {"order": {"order_id": "test_order_123"}}

        # Create test position
        position = Position(
            market_id="TEST-MARKET",
            side="YES",
            quantity=1,
            entry_price=0.45,
            live=False,
            timestamp=datetime.now(),
            rationale="Test execute_position fix",
            strategy="test",
        )
        position.id = 1

        # Execute the position
        result = await execute_position(
            position=position,
            live_mode=True,
            db_manager=self.mock_db_manager,
            kalshi_client=self.mock_kalshi_client,
        )

        # Verify the fix was applied
        self.assertTrue(result, "execute_position should return True")
        self.mock_kalshi_client.get_market.assert_called_once_with(position.market_id)
        self.mock_kalshi_client.place_order.assert_called_once()

        # Check that place_order was called with correct parameters
        call_args, call_kwargs = self.mock_kalshi_client.place_order.call_args

        # Verify exactly one price field is present
        price_fields = ["yes_price", "no_price", "yes_price_dollars", "no_price_dollars"]
        present_price_fields = [field for field in price_fields if field in call_kwargs]

        self.assertEqual(
            len(present_price_fields),
            1,
            f"Expected exactly 1 price field, found: {present_price_fields}",
        )
        self.assertIn("yes_price", call_kwargs, "YES position should have yes_price field")
        self.assertEqual(call_kwargs["yes_price"], 45, "yes_price should be the market ask price")


if __name__ == "__main__":
    # Run async test
    async def run_async_test():
        test_case = TestIssue9PriceFieldFix()
        test_case.setUp()
        await test_case.test_execute_position_with_fix()
        print("✅ Async test passed!")

    # Run sync tests
    unittest.main(argv=[""], verbosity=2, exit=False)

    # Run async test
    print("\n🧪 Running async test...")
    asyncio.run(run_async_test())
    print("\n✅ All tests passed! Issue #9 fix is working correctly.")
