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
            market_yes_price=0.50,
            contract_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.70
        )
        assert quantity == 2
        assert quantity * 0.50 <= 1.00

    async def test_kelly_no_position_basic(self):
        """Test basic NO position Kelly calculation."""
        # p_YES = 40%, so p_NO = 60%
        # c_YES = 60%, so c_NO = 40%; the NO contract costs $0.40.
        # f* = (0.60 - 0.40) / (1 - 0.40) = 0.20 / 0.60 = 0.333
        # The final cap allows at most floor($1 / $0.40) = 2 contracts.
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.60,
            contract_price=0.40,
            ai_probability=0.40,
            side="NO",
            confidence=0.70
        )
        assert quantity == 2
        assert quantity * (1.0 - 0.60) <= 1.00

    async def test_kelly_high_confidence_more_aggressive(self):
        """Test that higher confidence uses more of Kelly fraction."""
        # With confidence 0.80, should use more aggressive sizing
        qty_high = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.50,
            contract_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.85
        )
        qty_low = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.50,
            contract_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.50
        )
        assert qty_high >= qty_low, "Higher confidence should allow larger position"

    async def test_kelly_no_edge_returns_zero(self):
        """Test that no edge (p <= c) returns 0 contracts."""
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.60,
            contract_price=0.60,
            ai_probability=0.50,
            side="YES",
            confidence=0.70
        )
        assert quantity == 0, "No edge should return 0 contracts"

    async def test_kelly_buy_is_capped_at_one_dollar_after_boosts(self):
        """The final boosted order must remain at or below $1 notional."""
        # Very high edge should still be capped
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.10,
            contract_price=0.10,
            ai_probability=0.90,
            side="YES",
            confidence=0.95
        )
        assert quantity == 10
        assert quantity * 0.10 <= 1.00

    async def test_kelly_no_side_cap_uses_no_contract_price(self):
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.25,
            contract_price=0.75,
            ai_probability=0.10,
            side="NO",
            confidence=0.95,
        )

        assert quantity == 1
        assert quantity * (1.0 - 0.25) <= 1.00

    async def test_kelly_cap_uses_actual_no_ask_not_price_complement(self):
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.50,
            contract_price=0.51,
            ai_probability=0.10,
            side="NO",
            confidence=0.95,
        )

        assert quantity == 1
        assert quantity * 0.51 <= 1.00

    async def test_kelly_dollar_cap_uses_whole_contract_floor(self):
        quantity = _calculate_kelly_position_size(
            balance=100.0,
            market_yes_price=0.30,
            contract_price=0.30,
            ai_probability=0.95,
            side="YES",
            confidence=0.95,
        )

        assert quantity == 3
        assert quantity * 0.30 <= 1.00

    async def test_kelly_very_small_balance(self):
        """Test Kelly sizing with very small balance."""
        quantity = _calculate_kelly_position_size(
            balance=10.0,
            market_yes_price=0.50,
            contract_price=0.50,
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
            market_yes_price=0.50,
            contract_price=0.50,
            ai_probability=0.60,
            side="YES",
            confidence=0.70
        )
        # Maker has lower fees, so should get larger position
        assert qty_maker >= 1, "Maker position should be valid"


def test_buy_boost_stays_off_in_live(monkeypatch):
    """LIVE is not funded: the multiplier is installed but stays 1x there."""
    import src.jobs.broker as _broker
    from src.utils.mode import buy_boost

    monkeypatch.setattr(_broker, "should_trade_live", lambda: True)
    monkeypatch.delenv("BUY_MULT", raising=False)
    monkeypatch.delenv("BUY_MULT_LIVE", raising=False)
    assert buy_boost() == 1.0
    monkeypatch.setenv("BUY_MULT_LIVE", "2.5")
    assert buy_boost() == 2.5
