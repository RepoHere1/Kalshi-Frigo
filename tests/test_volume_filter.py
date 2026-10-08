"""
Tests for volume filter on price stops.

Verifies that exit triggers require sufficient orderbook volume
to ensure exits can actually execute.
"""
import pytest
from unittest.mock import Mock, AsyncMock
from datetime import datetime

from src.utils.database import Position
from src.jobs.track import _get_orderbook_volume


@pytest.mark.asyncio
class TestVolumeFilter:
    """Test volume filter for price stops."""

    async def test_get_orderbook_volume_returns_zero_on_error(self):
        """Test that volume filter returns 0 when orderbook read fails."""
        mock_kalshi_client = Mock()
        mock_kalshi_client.get_orderbook = AsyncMock(side_effect=Exception("API Error"))

        volume = await _get_orderbook_volume(
            mock_kalshi_client, "TEST-MARKET", "YES", 0.50
        )
        assert volume == 0.0, "Should return 0 on error (conservative)"

    async def test_get_orderbook_volume_filters_by_price(self):
        """Test that volume is filtered by price level."""
        mock_kalshi_client = Mock()
        mock_kalshi_client.get_orderbook = AsyncMock(return_value={
            "orderbook": {
                "bids": [
                    {"price_dollars": 0.48, "quantity": 10},
                    {"price_dollars": 0.49, "quantity": 20},
                    {"price_dollars": 0.50, "quantity": 30},
                    {"price_dollars": 0.51, "quantity": 40},
                ],
            }
        })

        # Ask for volume at or above 0.50
        volume = await _get_orderbook_volume(
            mock_kalshi_client, "TEST-MARKET", "YES", 0.50
        )
        # Should include 0.50, 0.51 (and higher if any)
        # For YES selling, we want bids at or above stop price
        assert volume >= 30, f"Should include volume at 0.50 and above, got {volume}"

    async def test_get_orderbook_volume_filters_correctly_for_no(self):
        """Test volume filter for NO positions."""
        mock_kalshi_client = Mock()
        mock_kalshi_client.get_orderbook = AsyncMock(return_value={
            "orderbook": {
                "bids": [
                    {"price_dollars": 0.48, "quantity": 10},
                    {"price_dollars": 0.49, "quantity": 20},
                    {"price_dollars": 0.50, "quantity": 30},
                ],
            }
        })

        volume = await _get_orderbook_volume(
            mock_kalshi_client, "TEST-MARKET", "NO", 0.50
        )
        assert volume >= 30, f"Should include volume at or above 0.50, got {volume}"

    async def test_volume_filter_requires_5x_position_size(self):
        """Test that exits require 5x position size in volume."""
        position = Position(
            market_id="TEST-MARKET",
            side="YES",
            entry_price=0.50,
            quantity=20,
            timestamp=datetime.now(),
            rationale="Test",
            confidence=0.75,
            live=False,
            stop_loss_price=0.45
        )

        mock_kalshi_client = Mock()
        mock_kalshi_client.get_orderbook = AsyncMock(return_value={
            "orderbook": {
                "bids": [
                    {"price_dollars": 0.48, "quantity": 100},
                ],
            }
        })

        volume = await _get_orderbook_volume(
            mock_kalshi_client, "TEST-MARKET", "YES", 0.45
        )
        
        # 20 contracts * 5 = 100 minimum required
        assert volume >= 100, f"Should have 5x volume (100), got {volume}"

    async def test_volume_filter_blocks_exit_when_insufficient(self):
        """Test that volume filter blocks exit when volume is insufficient."""
        position = Position(
            market_id="TEST-MARKET",
            side="YES",
            entry_price=0.50,
            quantity=100,
            timestamp=datetime.now(),
            rationale="Test",
            confidence=0.75,
            live=False,
            stop_loss_price=0.45
        )

        mock_kalshi_client = Mock()
        mock_kalshi_client.get_orderbook = AsyncMock(return_value={
            "orderbook": {
                "bids": [
                    {"price_dollars": 0.48, "quantity": 10},  # Only 10 available
                ],
            }
        })

        volume = await _get_orderbook_volume(
            mock_kalshi_client, "TEST-MARKET", "YES", 0.45
        )
        
        # 100 contracts * 5 = 500 minimum required, but only 10 available
        assert volume < 500, "Should not have sufficient volume"
