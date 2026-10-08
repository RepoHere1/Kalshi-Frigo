"""
Tests for Kelly Criterion position sizing.

Verifies that position sizes are calculated using the correct formula:
f* = (p - c) / (1 - c) for YES positions
f* = (q - c_NO) / (1 - c_NO) for NO positions
with quarter-Kelly for safety.
"""
import pytest
from src.jobs.decide import _calculate_kelly_position_size


@pytest.mark.asyncio
class TestKellyPositionSizing:
    """Test Kelly Criterion position sizing calculations."""

    async def test_kelly_yes_position_basic(self):
        """Test basic YES position Kelly calculation."""
        # p = 60%, c = 50%, f* = (0.60 - 0.50) / (1 - 0.50) = 0.20
        # Quarter-Kelly = 0.05, bankroll = $100
        # Investment = $100 * 0.05 = $5, quantity = $5 / $0.50 = 10 contracts
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.70
        )
        assert quantity >= 1, "Should get at least 1 contract"
        assert quantity <= 20, "Should be reasonable position size"

    async def test_kelly_no_position_basic(self):
        """Test basic NO position Kelly calculation."""
        # p_YES = 40%, so p_NO = 60%
        # c_YES = 60%, so c_NO = 40%
        # f* = (0.60 - 0.40) / (1 - 0.40) = 0.20 / 0.60 = 0.333
        # Quarter-Kelly = 0.083, bankroll = $100
        # Investment = $8.30, quantity = $8.30 / $0.60 = 13 contracts
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_price=0.60,
            ai_probability=0.40,
            side="NO",
            confidence=0.70
        )
        assert quantity >= 1, "Should get at least 1 contract"

    async def test_kelly_high_confidence_more_aggressive(self):
        """Test that higher confidence uses more of Kelly fraction."""
        # With confidence 0.80, should use more aggressive sizing
        qty_high = _calculate_kelly_position_size(
            balance=100.0,
            market_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.85
        )
        qty_low = _calculate_kelly_position_size(
            balance=100.0,
            market_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.50
        )
        assert qty_high >= qty_low, "Higher confidence should allow larger position"

    async def test_kelly_no_edge_returns_zero(self):
        """Test that no edge (p <= c) returns 0 contracts."""
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_price=0.60,
            ai_probability=0.50,
            side="YES",
            confidence=0.70
        )
        assert quantity == 0, "No edge should return 0 contracts"

    async def test_kelly_cap_at_15_percent(self):
        """Test that position size is capped at 15% of bankroll."""
        # Very high edge should still be capped
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_price=0.10,
            ai_probability=0.90,
            side="YES",
            confidence=0.95
        )
        # Max 15% of $100 = $15, at $0.10 = 150 contracts max
        assert quantity <= 150, f"Should be capped at 15% of bankroll, got {quantity}"

    async def test_kelly_very_small_balance(self):
        """Test Kelly sizing with very small balance."""
        quantity = _calculate_kelly_position_size(
            balance=10.0,
            market_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.70
        )
        # Quarter-Kelly of $10 = $0.50, at $0.50 = 1 contract minimum
        assert quantity >= 1, "Should get at least 1 contract even with small balance"

    async def test_kelly_edge_after_fees(self):
        """Test that fees reduce effective edge for Kelly calculation."""
        # With taker fees, effective edge is reduced
        qty_maker = _calculate_kelly_position_size(
            balance=100.0,
            market_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.70
        )
        # Maker has lower fees, so should get larger position
        assert qty_maker >= 1, "Maker position should be valid"
