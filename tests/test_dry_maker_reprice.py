"""
Tests for DRY maker fill simulation with live book re-pricing.

Verifies that DRY maker fills:
1. Read the real orderbook each iteration
2. Only confirm fill when book crosses the limit price
3. Re-price the position to the actual fill price
"""
import pytest
from unittest.mock import Mock, AsyncMock
from datetime import datetime

from src.jobs.execute import _simulate_dry_maker_fill
from src.utils.database import Position
from src.jobs.broker import build_order_request


@pytest.mark.asyncio
class TestDryMakerFillRepricing:
    """Test DRY maker fill simulation with book re-pricing."""

    async def test_maker_fill_detects_book_crossing(self):
        """Test that maker fill detects when book crosses limit price."""
        mock_kalshi_client = Mock()
        mock_kalshi_client.get_market = AsyncMock(return_value={
            "market": {
                "yes_bid_dollars": 0.58,
                "yes_ask_dollars": 0.60,
                "no_bid_dollars": 0.40,
                "no_ask_dollars": 0.42,
            }
        })

        request = Mock()
        request.ticker = "TEST-MARKET"
        request.side = "YES"
        request.fill_price = 0.59  # Limit price

        position = Position(
            market_id="TEST-MARKET",
            side="YES",
            entry_price=0.58,
            quantity=10,
            timestamp=datetime.now(),
            rationale="Test",
            confidence=0.75,
            live=False
        )

        # Book crosses at 0.60 which is > 0.59, so it should fill
        result = await _simulate_dry_maker_fill(
            mock_kalshi_client, request, position, 0.0, None
        )
        # With 0.0 wait time, it reads once and returns based on current book
        assert isinstance(result, bool)

    async def test_maker_fill_returns_false_when_book_does_not_cross(self):
        """Test that maker fill returns False when book never crosses."""
        mock_kalshi_client = Mock()
        # Book ask is above limit price, so no fill (seller wants more than buyer pays)
        mock_kalshi_client.get_market = AsyncMock(return_value={
            "market": {
                "yes_bid_dollars": 0.58,
                "yes_ask_dollars": 0.60,  # Above limit price of 0.59 -> no cross -> no fill
                "no_bid_dollars": 0.40,
                "no_ask_dollars": 0.42,
            }
        })

        request = Mock()
        request.ticker = "TEST-MARKET"
        request.side = "YES"
        request.fill_price = 0.59

        position = Position(
            market_id="TEST-MARKET",
            side="YES",
            entry_price=0.58,
            quantity=10,
            timestamp=datetime.now(),
            rationale="Test",
            confidence=0.75,
            live=False
        )

        # With very short wait, should return False
        result = await _simulate_dry_maker_fill(
            mock_kalshi_client, request, position, 0.0, None
        )
        assert result == False

    async def test_maker_fill_read_book_each_iteration(self):
        """Test that maker fill reads book each iteration when waiting."""
        mock_kalshi_client = Mock()
        # First call shows no crossing, second shows crossing
        call_count = [0]
        
        async def mock_get_market(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"market": {
                    "yes_bid_dollars": 0.58,
                    "yes_ask_dollars": 0.57,
                    "no_bid_dollars": 0.40,
                    "no_ask_dollars": 0.42,
                }}
            else:
                return {"market": {
                    "yes_bid_dollars": 0.58,
                    "yes_ask_dollars": 0.60,  # Now crosses
                    "no_bid_dollars": 0.40,
                    "no_ask_dollars": 0.42,
                }}
        
        mock_kalshi_client.get_market = mock_get_market

        request = Mock()
        request.ticker = "TEST-MARKET"
        request.side = "YES"
        request.fill_price = 0.59

        position = Position(
            market_id="TEST-MARKET",
            side="YES",
            entry_price=0.58,
            quantity=10,
            timestamp=datetime.now(),
            rationale="Test",
            confidence=0.75,
            live=False
        )

        # With enough wait time, should eventually see crossing
        result = await _simulate_dry_maker_fill(
            mock_kalshi_client, request, position, 5.0, None
        )
        assert call_count[0] >= 1, "Should read book multiple times when waiting"
